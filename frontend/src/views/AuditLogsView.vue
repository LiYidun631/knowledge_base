<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Download, Refresh } from '@element-plus/icons-vue'
import { client, errorMessage, errorText } from '../api/client'
import PageHeader from '../components/ui/PageHeader.vue'
import DataPanel from '../components/ui/DataPanel.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import StatusPill from '../components/ui/StatusPill.vue'
import EmptyState from '../components/ui/EmptyState.vue'

interface AuditLogItem {
  id: string
  actor_name: string
  actor_role: string
  credential_id: string | null
  method: string
  path: string
  status_code: number
  duration_ms: number
  client_ip: string | null
  created_at: string
}

interface AuditLogPage {
  page: number
  page_size: number
  total: number
  items: AuditLogItem[]
}

const AUDITED_METHODS = ['POST', 'PUT', 'PATCH', 'DELETE'] as const
type AuditedMethod = (typeof AUDITED_METHODS)[number]

const roleLabels: Record<string, string> = {
  admin: '管理员',
  operator: '运营',
  viewer: '只读',
}

const rows = ref<AuditLogItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const loading = ref(false)
const exporting = ref(false)
const loadError = ref('')

const filters = reactive({
  actorName: '',
  method: '' as '' | AuditedMethod,
  path: '',
  dateRange: null as [string, string] | null,
})

const hasFilters = computed(
  () =>
    Boolean(filters.actorName.trim()) ||
    Boolean(filters.method) ||
    Boolean(filters.path.trim()) ||
    Boolean(filters.dateRange),
)

// 后端时间均为 UTC；按 UTC 整日边界换算，避免本地时区把跨日记录筛错。
function buildParams(): Record<string, unknown> {
  const params: Record<string, unknown> = {}
  if (filters.actorName.trim()) params.actor_name = filters.actorName.trim()
  if (filters.method) params.method = filters.method
  if (filters.path.trim()) params.path = filters.path.trim()
  if (filters.dateRange) {
    params.start_time = `${filters.dateRange[0]}T00:00:00Z`
    params.end_time = `${filters.dateRange[1]}T23:59:59Z`
  }
  return params
}

async function load(silent = false) {
  loading.value = true
  try {
    const { data } = await client.get<AuditLogPage>('/admin/audit-logs', {
      params: { ...buildParams(), page: page.value, page_size: pageSize.value },
    })
    rows.value = data.items
    total.value = data.total
    loadError.value = ''
  } catch (error) {
    loadError.value = errorText(error, '加载审计日志失败')
    rows.value = []
    total.value = 0
    if (!silent) errorMessage(error, '加载审计日志失败')
  } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}

function resetFilters() {
  filters.actorName = ''
  filters.method = ''
  filters.path = ''
  filters.dateRange = null
  page.value = 1
  load()
}

function onPageChange(next: number) {
  page.value = next
  load()
}

async function exportCsv() {
  exporting.value = true
  try {
    const { data } = await client.get<Blob>('/admin/audit-logs/export', {
      params: buildParams(),
      responseType: 'blob',
    })
    const url = URL.createObjectURL(data)
    const link = document.createElement('a')
    const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '')
    link.href = url
    link.download = `audit-logs-${stamp}.csv`
    link.click()
    URL.revokeObjectURL(url)
    ElMessage.success(`已导出 ${total.value} 条`)
  } catch (error) {
    errorMessage(error, '导出审计日志失败')
  } finally {
    exporting.value = false
  }
}

function methodTone(method: string): string {
  return `method-${method.toLowerCase()}`
}

function formatDateTime(value: string): string {
  return value.slice(0, 19).replace('T', ' ')
}

onMounted(() => load())
</script>

<template>
  <div class="audit-page">
    <PageHeader
      title="审计日志"
      eyebrow="操作留痕"
      subtitle="只记录管理端写操作（POST / PUT / PATCH / DELETE）· 时间均为 UTC · 保留 180 天"
    >
      <template #meta>
        <span class="header-count">共 {{ total.toLocaleString('zh-CN') }} 条</span>
      </template>
      <template #actions>
        <el-button type="primary" size="small" :icon="Download" :loading="exporting" @click="exportCsv">
          导出 CSV
        </el-button>
        <button class="refresh-btn" type="button" title="刷新" @click="load()">
          <el-icon :class="{ spinning: loading }"><Refresh /></el-icon>
        </button>
      </template>
    </PageHeader>

    <DataPanel flush>
      <div class="panel-toolbar">
        <FilterBar>
          <el-input
            v-model="filters.actorName"
            clearable
            placeholder="操作者"
            class="actor-input"
            @keyup.enter="search"
          />
          <el-select v-model="filters.method" clearable placeholder="全部方法" class="method-select">
            <el-option v-for="method in AUDITED_METHODS" :key="method" :label="method" :value="method" />
          </el-select>
          <el-input
            v-model="filters.path"
            clearable
            placeholder="路径包含，如 /documents"
            class="path-input"
            @keyup.enter="search"
          />
          <el-date-picker
            v-model="filters.dateRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            range-separator="至"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            class="date-range"
          />
          <template #actions>
            <el-button type="primary" size="small" @click="search">查询</el-button>
            <el-button size="small" :disabled="!hasFilters" @click="resetFilters">重置</el-button>
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

      <el-table :data="rows" v-loading="loading" class="audit-table" row-key="id">
        <template #empty>
          <EmptyState
            :title="hasFilters ? '没有匹配的审计记录' : '还没有写操作记录'"
            :text="
              hasFilters
                ? '换个操作者、路径或时间范围再试试。'
                : '管理员在管理端执行写操作后，这里会留下留痕。'
            "
            :muted="hasFilters"
          />
        </template>
        <el-table-column label="时间(UTC)" width="168">
          <template #default="{ row }">
            <span class="mono">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作者" min-width="170">
          <template #default="{ row }">
            <span class="actor-name">{{ row.actor_name }}</span>
            <span class="role-chip" :class="`role-${row.actor_role}`">
              {{ roleLabels[row.actor_role] ?? row.actor_role }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="方法" width="92">
          <template #default="{ row }">
            <span class="method-chip" :class="methodTone(row.method)">{{ row.method }}</span>
          </template>
        </el-table-column>
        <el-table-column label="路径" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="path-text">{{ row.path }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="84">
          <template #default="{ row }">
            <StatusPill
              :label="String(row.status_code)"
              :tone="row.status_code < 400 ? 'success' : 'danger'"
              :dot="false"
            />
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="88" align="right">
          <template #default="{ row }">
            <span class="mono">{{ row.duration_ms }} ms</span>
          </template>
        </el-table-column>
        <el-table-column label="客户端 IP" width="130">
          <template #default="{ row }">
            <span class="mono">{{ row.client_ip || '—' }}</span>
          </template>
        </el-table-column>
      </el-table>

      <template #footer>
        <el-pagination
          layout="prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          @current-change="onPageChange"
        />
      </template>
    </DataPanel>
  </div>
</template>

<style scoped>
.audit-page {
  min-height: 100%;
  /* 与统计概览/任务中心一致的极淡靛蓝氛围。 */
  background: radial-gradient(720px 260px at 86% -60px, rgba(79, 70, 229, 0.07), transparent 70%);
}
.header-count {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
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
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
.panel-toolbar {
  padding: 14px 18px 0;
}
.actor-input {
  width: 150px;
}
.method-select {
  width: 118px;
}
.path-input {
  width: 220px;
}
.date-range {
  width: 268px;
}
.load-error {
  margin: 0 18px 12px;
}
.actor-name {
  font-size: 13px;
  color: var(--el-text-color-primary);
}
.role-chip {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: var(--kb-font-2xs);
  line-height: 1.6;
  background: #f2f3f5;
  color: #6b7280;
}
.role-chip.role-admin {
  background: #eef0fd;
  color: #4f46e5;
}
.role-chip.role-operator {
  background: #e8f4f1;
  color: #0f766e;
}
.method-chip {
  display: inline-flex;
  padding: 1px 8px;
  border-radius: 4px;
  font-size: var(--kb-font-2xs);
  font-weight: 600;
  letter-spacing: 0.3px;
  font-family: 'Cascadia Mono', Consolas, monospace;
}
.method-post {
  color: #4f46e5;
  background: rgba(79, 70, 229, 0.09);
}
.method-put {
  color: #b45309;
  background: rgba(180, 83, 9, 0.09);
}
.method-patch {
  color: #0f766e;
  background: rgba(15, 118, 110, 0.09);
}
.method-delete {
  color: #dc2626;
  background: rgba(220, 38, 38, 0.09);
}
.path-text {
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 12px;
  color: var(--el-text-color-regular);
}
.mono {
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 12px;
}
.audit-table :deep(.el-table__row) {
  animation: row-in 0.24s var(--kb-ease) backwards;
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
</style>
