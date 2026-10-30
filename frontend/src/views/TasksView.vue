<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { client, errorMessage, errorText } from '../api/client'
import { canWrite } from '../api/session'
import PageHeader from '../components/ui/PageHeader.vue'
import DataPanel from '../components/ui/DataPanel.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import StatusPill from '../components/ui/StatusPill.vue'
import EmptyState from '../components/ui/EmptyState.vue'

interface Company {
  id: string
  name: string
}

interface KnowledgeBase {
  id: string
  name: string
}

type TaskKind = 'ingest' | 'faq' | 'evaluation'
type TaskStatus = 'queued' | 'running' | 'failed' | 'cancelled' | 'success'
type StatusFilter = 'open' | 'queued' | 'running' | 'failed' | 'done' | 'all'

interface TaskItem {
  kind: TaskKind
  task_id: string
  status: TaskStatus
  title: string
  detail: string | null
  company_id: string | null
  company_name: string | null
  knowledge_base_id: string | null
  knowledge_base_name: string | null
  document_id: string | null
  attempts: number
  progress_current: number | null
  progress_total: number | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  error_message: string | null
}

interface TaskCounts {
  queued: number
  running: number
  failed: number
  done_today: number
}

interface TaskListResponse {
  items: TaskItem[]
  counts: TaskCounts
  truncated: boolean
  generated_at: string
}

const router = useRouter()
const companies = ref<Company[]>([])
const kbs = ref<KnowledgeBase[]>([])
const rows = ref<TaskItem[]>([])
const counts = ref<TaskCounts>({ queued: 0, running: 0, failed: 0, done_today: 0 })
const loading = ref(false)
const autoRefresh = ref(true)
const truncated = ref(false)
const generatedAt = ref('')
const loadError = ref('')
const actionKey = ref('')

const filters = reactive({
  kind: '' as '' | TaskKind,
  status: 'open' as StatusFilter,
  companyId: '',
  kbId: '',
})

const kindMeta: Record<TaskKind, { label: string; tone: string }> = {
  ingest: { label: '入库', tone: 'ingest' },
  faq: { label: 'FAQ', tone: 'faq' },
  evaluation: { label: '评测', tone: 'evaluation' },
}

type PillTone = 'success' | 'warning' | 'danger' | 'info' | 'primary'

const statusMeta: Record<TaskStatus, { label: string; tone: PillTone }> = {
  queued: { label: '排队中', tone: 'warning' },
  running: { label: '进行中', tone: 'primary' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已取消', tone: 'info' },
  success: { label: '已完成', tone: 'success' },
}

// 状态瓷砖即筛选器，点击切换；数量始终来自后端 counts，不受当前状态过滤影响。
const tiles = computed(() => [
  { key: 'queued' as StatusFilter, label: '排队中', value: counts.value.queued, tone: 'queued' },
  { key: 'running' as StatusFilter, label: '进行中', value: counts.value.running, tone: 'running' },
  { key: 'failed' as StatusFilter, label: '失败待处理', value: counts.value.failed, tone: 'failed' },
  { key: 'done' as StatusFilter, label: '今日完成', value: counts.value.done_today, tone: 'done' },
])

const activeTasks = computed(() => counts.value.queued + counts.value.running)
// 轮询只在有排队/进行中任务时开启，所以「空闲」和「已关闭」要说清楚，不能让开关看起来失灵。
const liveLabel = computed(() => {
  if (!autoRefresh.value) return '自动刷新已关闭'
  return activeTasks.value > 0 ? '实时刷新中' : '空闲 · 有任务时自动刷新'
})
const emptyText = computed(() =>
  filters.status === 'open'
    ? '当前没有未完成的任务'
    : filters.status === 'done'
      ? '今天还没有完成的任务'
      : '没有符合筛选条件的任务',
)
// 默认视图（未完成）本身就是一种筛选，不算用户主动收窄。
const filtersActive = computed(
  () =>
    Boolean(filters.kind || filters.companyId || filters.kbId) ||
    filters.status !== 'open',
)

// 瓷砖直接改状态筛选，需显式重新拉取（下拉框另有 @change）。
function pickStatus(key: StatusFilter) {
  const next = filters.status === key ? 'open' : key
  if (next === filters.status) return
  filters.status = next
  load()
}

async function loadCompanies() {
  try {
    const { data } = await client.get<Company[]>('/companies')
    companies.value = data
  } catch (error) {
    errorMessage(error, '加载公司列表失败')
  }
}

async function loadKbs() {
  kbs.value = []
  if (!filters.companyId) return
  try {
    const { data } = await client.get<KnowledgeBase[]>('/knowledge-bases', {
      params: { company_id: filters.companyId, include_archived: true, limit: 200 },
    })
    kbs.value = data
  } catch (error) {
    errorMessage(error, '加载知识库列表失败')
  }
}

async function load(silent = false) {
  if (loading.value) return
  loading.value = true
  try {
    const params: Record<string, unknown> = { status: filters.status }
    if (filters.kind) params.kind = filters.kind
    if (filters.companyId) params.company_id = filters.companyId
    if (filters.kbId) params.knowledge_base_id = filters.kbId
    const { data } = await client.get<TaskListResponse>('/admin/tasks', { params })
    rows.value = data.items
    counts.value = data.counts
    truncated.value = data.truncated
    generatedAt.value = data.generated_at
    loadError.value = ''
  } catch (error) {
    // 请求失败时不能让空表格看起来像「没有任务」，在列表上方明确提示。
    loadError.value = errorText(error, '加载任务列表失败')
    if (!silent) errorMessage(error, '加载任务列表失败')
  } finally {
    loading.value = false
  }
}

let timer: number | undefined

// 只在有排队/进行中的任务时轮询，页面空闲时不打扰后端。
watch(
  () => [autoRefresh.value, activeTasks.value] as const,
  ([enabled, active]) => {
    window.clearInterval(timer)
    timer = undefined
    if (!enabled || active === 0) return
    timer = window.setInterval(() => load(true), 8000)
  },
  { immediate: true },
)

watch(
  () => filters.companyId,
  async () => {
    filters.kbId = ''
    await loadKbs()
    load()
  },
)

async function retryIngest(task: TaskItem) {
  if (!task.document_id) return
  actionKey.value = `ingest-${task.task_id}`
  try {
    await client.post(`/documents/${task.document_id}/retry`)
    ElMessage.success('已重新排队，稍后自动刷新')
    await load()
  } catch (error) {
    errorMessage(error, '重试失败')
  } finally {
    actionKey.value = ''
  }
}

async function regenerateFaq(task: TaskItem) {
  if (!task.document_id) return
  actionKey.value = `faq-${task.task_id}`
  try {
    const { data } = await client.post<{ entries: number }>(
      `/documents/${task.document_id}/faqs/generate`,
    )
    ElMessage.success(`已生成 ${data.entries} 条 FAQ`)
    await load()
  } catch (error) {
    errorMessage(error, '重新生成 FAQ 失败')
  } finally {
    actionKey.value = ''
  }
}

function formatDateTime(value: string | null): string {
  if (!value) return '—'
  return value.slice(0, 19).replace('T', ' ')
}

function formatMs(value: number | null): string {
  if (value === null || value === undefined) return '—'
  if (value < 1000) return `${value} ms`
  if (value < 60_000) return `${(value / 1000).toFixed(1)} s`
  return `${Math.floor(value / 60_000)} 分 ${Math.round((value % 60_000) / 1000)} 秒`
}

function updatedLabel(value: string): string {
  return value ? value.slice(11, 19) : '—'
}

function progressPercent(task: TaskItem): number {
  const total = task.progress_total ?? 0
  if (!total) return 0
  return Math.min(100, Math.round(((task.progress_current ?? 0) / total) * 100))
}

onMounted(async () => {
  await loadCompanies()
  await load()
})

onBeforeUnmount(() => window.clearInterval(timer))
</script>

<template>
  <div class="tasks-page">
    <PageHeader
      title="任务中心"
      eyebrow="运行队列"
      subtitle="入库、FAQ 生成与检索评测的统一视图"
    >
      <template #meta>
        <span class="header-count">更新于 {{ updatedLabel(generatedAt) }}</span>
      </template>
      <template #actions>
        <span class="live-hint" :class="{ live: autoRefresh && activeTasks > 0 }">
          <i class="live-dot" />
          {{ liveLabel }}
        </span>
        <el-switch v-model="autoRefresh" size="small" />
        <button class="refresh-btn" type="button" title="刷新" @click="load()">
          <el-icon :class="{ spinning: loading }"><Refresh /></el-icon>
        </button>
      </template>
    </PageHeader>

    <section class="tile-grid">
      <button
        v-for="(tile, index) in tiles"
        :key="tile.key"
        type="button"
        class="stat-tile"
        :class="[`tone-${tile.tone}`, { active: filters.status === tile.key }]"
        :style="{ '--delay': `${index * 40}ms` }"
        @click="pickStatus(tile.key)"
      >
        <span class="tile-mark" aria-hidden="true" />
        <span class="tile-label">{{ tile.label }}</span>
        <span class="tile-value">{{ tile.value }}</span>
      </button>
    </section>

    <DataPanel flush>
      <div class="panel-toolbar">
        <FilterBar>
          <el-select v-model="filters.kind" clearable placeholder="全部类型" class="kind-select" @change="load()">
            <el-option label="入库" value="ingest" />
            <el-option label="FAQ 生成" value="faq" />
            <el-option label="检索评测" value="evaluation" />
          </el-select>
          <el-select v-model="filters.status" class="status-select" @change="load()">
            <el-option label="未完成" value="open" />
            <el-option label="排队中" value="queued" />
            <el-option label="进行中" value="running" />
            <el-option label="失败" value="failed" />
            <el-option label="今日完成" value="done" />
            <el-option label="全部" value="all" />
          </el-select>
          <el-select v-model="filters.companyId" clearable placeholder="全部公司" class="company-select">
            <el-option v-for="company in companies" :key="company.id" :label="company.name" :value="company.id" />
          </el-select>
          <el-select
            v-model="filters.kbId"
            clearable
            placeholder="全部知识库"
            class="kb-select"
            :disabled="!filters.companyId"
            @change="load()"
          >
            <el-option v-for="kb in kbs" :key="kb.id" :label="kb.name" :value="kb.id" />
          </el-select>
          <template #actions>
            <span class="row-hint">
              共 {{ rows.length }} 条{{ truncated ? '（已截断，请缩小筛选范围）' : '' }}
            </span>
          </template>
        </FilterBar>
      </div>

      <el-alert
        v-if="loadError"
        class="load-error"
        type="error"
        :closable="false"
        show-icon
        :title="loadError"
      />

      <el-table :data="rows" v-loading="loading" class="task-table" row-key="task_id">
        <template #empty>
          <EmptyState
            :title="emptyText"
            :text="
              filtersActive
                ? '调整筛选条件，或稍后回来重新查看。'
                : '入库、FAQ 生成与评测任务提交后会实时出现在这里。'
            "
            :muted="filtersActive"
          />
        </template>
        <el-table-column label="类型" width="86">
          <template #default="{ row }">
            <span class="kind-chip" :class="`tone-${kindMeta[row.kind as TaskKind].tone}`">
              {{ kindMeta[row.kind as TaskKind].label }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="104">
          <template #default="{ row }">
            <StatusPill
              :label="statusMeta[row.status as TaskStatus].label"
              :tone="statusMeta[row.status as TaskStatus].tone"
              :pulse="row.status === 'running'"
            />
          </template>
        </el-table-column>
        <el-table-column label="任务对象" min-width="260">
          <template #default="{ row }">
            <div class="task-title">{{ row.title }}</div>
            <div v-if="row.detail" class="task-detail">{{ row.detail }}</div>
            <div v-if="row.kind === 'evaluation' && row.progress_total" class="progress-line">
              <i :style="{ width: `${progressPercent(row)}%` }" />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="公司" width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.company_name ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="知识库" width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.knowledge_base_name ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="开始时间" width="150">
          <template #default="{ row }">{{ formatDateTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="96" align="right">
          <template #default="{ row }">
            <span class="mono">{{ formatMs(row.duration_ms) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="错误摘要" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.error_message" class="error-text">{{ row.error_message }}</span>
            <span v-else class="cell-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="112" align="right">
          <template #default="{ row }">
            <el-button
              v-if="canWrite && row.kind === 'ingest' && row.status === 'failed'"
              size="small"
              type="primary"
              plain
              :loading="actionKey === `ingest-${row.task_id}`"
              @click="retryIngest(row)"
            >
              重试入库
            </el-button>
            <el-button
              v-else-if="canWrite && row.kind === 'faq' && row.status === 'failed'"
              size="small"
              type="primary"
              plain
              :loading="actionKey === `faq-${row.task_id}`"
              @click="regenerateFaq(row)"
            >
              重新生成
            </el-button>
            <el-button
              v-else-if="row.kind === 'evaluation'"
              size="small"
              plain
              @click="router.push('/evaluation')"
            >
              查看评测
            </el-button>
            <span v-else class="cell-muted">—</span>
          </template>
        </el-table-column>
      </el-table>
    </DataPanel>
  </div>
</template>

<style scoped>
.tasks-page {
  min-height: 100%;
  /* 与统计概览一致的极淡靛蓝氛围。 */
  background: radial-gradient(720px 260px at 86% -60px, rgba(79, 70, 229, 0.07), transparent 70%);
}
.header-count {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}
.live-hint {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  font-size: var(--kb-font-2xs);
  color: var(--el-text-color-placeholder);
  white-space: nowrap;
}
.live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--el-border-color-dark);
}
.live-hint.live {
  color: var(--el-color-primary);
}
.live-hint.live .live-dot {
  background: var(--el-color-primary);
  animation: live-breathe 1.8s var(--kb-ease) infinite;
}
/* 与头部小号按钮同高，保持动作区一行对齐。 */
.refresh-btn {
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: #fff;
  color: var(--el-text-color-secondary);
  cursor: pointer;
  transition: color 0.2s var(--kb-ease), border-color 0.2s var(--kb-ease);
}
.refresh-btn:hover {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary-light-7);
}
.refresh-btn .el-icon {
  font-size: 13px;
}
.spinning {
  animation: spin 0.9s linear infinite;
}

/* —— 状态瓷砖：点击即筛选，尺寸与指标卡一致 —— */
.tile-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}
.stat-tile {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 2px;
  height: 96px;
  padding: 13px 16px;
  text-align: left;
  background: var(--kb-panel-bg);
  border: 1px solid var(--kb-panel-border);
  border-radius: var(--kb-panel-radius);
  box-shadow: var(--kb-panel-shadow);
  cursor: pointer;
  overflow: hidden;
  animation: tile-rise 0.3s var(--kb-ease) backwards;
  animation-delay: var(--delay);
  transition: transform 0.15s var(--kb-ease), border-color 0.2s var(--kb-ease),
    box-shadow 0.2s var(--kb-ease);
}
.stat-tile:hover {
  transform: translateY(-1px);
  box-shadow: 0 6px 16px rgba(15, 23, 42, 0.07);
}
.stat-tile:active {
  transform: scale(0.995);
}
.stat-tile.active {
  border-color: var(--tile-accent);
  box-shadow: inset 0 0 0 1px var(--tile-accent);
}
.tile-mark {
  position: absolute;
  top: 14px;
  left: 16px;
  width: 6px;
  height: 6px;
  border-radius: 2px;
  background: var(--tile-accent);
}
.tile-label {
  padding-left: 12px;
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
}
.tile-value {
  margin: auto 0 0;
  font-size: 24px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--el-text-color-primary);
  font-variant-numeric: tabular-nums;
  transition: color 0.2s var(--kb-ease);
}
.stat-tile.active .tile-value {
  color: var(--tile-accent);
}
.tone-queued {
  --tile-accent: #d97706;
  --tile-wash: rgba(217, 119, 6, 0.1);
}
.tone-running {
  --tile-accent: #4f46e5;
  --tile-wash: rgba(79, 70, 229, 0.1);
}
.tone-failed {
  --tile-accent: #dc2626;
  --tile-wash: rgba(220, 38, 38, 0.09);
}
.tone-done {
  --tile-accent: #16a34a;
  --tile-wash: rgba(22, 163, 74, 0.09);
}
/* 任务类型胶囊（入库 / FAQ / 评测） */
.tone-ingest {
  --tile-accent: #4f46e5;
  --tile-wash: rgba(79, 70, 229, 0.09);
}
.tone-faq {
  --tile-accent: #0284c7;
  --tile-wash: rgba(2, 132, 199, 0.09);
}
.tone-evaluation {
  --tile-accent: #7c3aed;
  --tile-wash: rgba(124, 58, 237, 0.09);
}

.panel-toolbar {
  padding: 14px 18px 0;
}
.load-error {
  margin: 0 18px 12px;
}
.kind-select {
  width: 120px;
}
.status-select {
  width: 118px;
}
.company-select {
  width: 160px;
}
.kb-select {
  width: 180px;
}
.row-hint {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-placeholder);
  white-space: nowrap;
}
.kind-chip {
  display: inline-flex;
  padding: 1px 8px;
  border-radius: 4px;
  font-size: var(--kb-font-2xs);
  line-height: 18px;
  color: var(--tile-accent);
  background: var(--tile-wash);
}
.task-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.task-detail {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.progress-line {
  margin-top: 6px;
  width: 180px;
  height: 3px;
  border-radius: 999px;
  background: var(--el-fill-color);
  overflow: hidden;
}
.progress-line i {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--el-color-primary);
  transition: width 0.4s var(--kb-ease);
}
.error-text {
  color: var(--el-color-danger);
  font-size: 12px;
}
.cell-muted {
  color: var(--el-text-color-placeholder);
}
.mono {
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 12px;
}
.task-table :deep(.el-table__row) {
  animation: row-in 0.24s var(--kb-ease) backwards;
}

@keyframes tile-rise {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
@keyframes row-in {
  from {
    opacity: 0;
    transform: translateX(-4px);
  }
  to {
    opacity: 1;
    transform: translateX(0);
  }
}
@keyframes live-breathe {
  0%,
  100% {
    opacity: 1;
    box-shadow: 0 0 0 0 rgba(79, 70, 229, 0.35);
  }
  50% {
    opacity: 0.55;
    box-shadow: 0 0 0 3px rgba(79, 70, 229, 0);
  }
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 900px) {
  .tile-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
