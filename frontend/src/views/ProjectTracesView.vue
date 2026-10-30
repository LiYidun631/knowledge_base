<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { client, errorMessage } from '../api/client'
import { canWrite } from '../api/session'
import { renderMarkdown } from '../utils/markdown'
import { resolveInlineImages } from '../utils/assets'
import {
  beijingDateKey,
  formatBeijingClock,
  formatBeijingDateTime,
  todayBeijingDateKey,
} from '../utils/datetime'
import PageHeader from '../components/ui/PageHeader.vue'
import DataPanel from '../components/ui/DataPanel.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import StatusPill from '../components/ui/StatusPill.vue'
import EmptyState from '../components/ui/EmptyState.vue'

interface Project {
  id: string
  project_key: string
  display_name: string
  description: string | null
  is_enabled: boolean
  retention_days: number
  created_at: string
  updated_at: string
}

interface Company {
  id: string
  name: string
}

interface KnowledgeBase {
  id: string
  name: string
}

interface RetrievalAttempt {
  index: number
  query: string
  retrieval_mode: string
  top_k: number
  product_name: string | null
  model_name: string | null
  result_count: number
  top_score: number | null
  duration_ms: number
  error_code?: string
  error_message?: string
}

interface ProjectTraceItem {
  id: string
  project_id: string
  project_key: string
  project_name: string
  company_id: string
  company_name: string | null
  knowledge_base_id: string
  knowledge_base_name: string
  external_request_id: string
  session_ref: string | null
  user_ref: string | null
  query: string
  status: 'running' | 'success' | 'no_results' | 'failed' | 'clarification'
  review_status: 'none' | 'pending' | 'resolved'
  feedback: 'up' | 'down' | null
  feedback_reason: string | null
  feedback_at: string | null
  answer_mode: string | null
  intent: Record<string, unknown> | null
  chat_provider: string | null
  chat_model_name: string | null
  retrieval_mode: string | null
  top_k: number | null
  include_context: boolean
  product_name: string | null
  model_name: string | null
  retrieval_attempts: RetrievalAttempt[]
  result_count: number
  source_count: number
  top_rerank_score: number | null
  retrieval_duration_ms: number | null
  generation_duration_ms: number | null
  total_duration_ms: number | null
  prompt_tokens: number | null
  completion_tokens: number | null
  total_tokens: number | null
  error_code: string | null
  error_message: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
  answer_preview: string | null
}

interface ProjectTraceSource {
  id: string
  attempt_index: number
  rank: number
  document_id: string | null
  document_version_id: string | null
  chunk_id: string | null
  filename: string
  version_number: number
  page_number: number | null
  section: string | null
  content_snapshot: string
  context_snapshot: string | null
  cosine_distance: number | null
  bm25_score: number | null
  rrf_score: number | null
  rerank_score: number | null
  asset_ids: string[]
  created_at: string
}

interface ProjectTraceDetail extends ProjectTraceItem {
  answer: string | null
  sources: ProjectTraceSource[]
}

interface SourceGroup {
  index: number
  attempt: RetrievalAttempt | null
  sources: ProjectTraceSource[]
}

const projects = ref<Project[]>([])
const companies = ref<Company[]>([])
const kbs = ref<KnowledgeBase[]>([])
const rows = ref<ProjectTraceItem[]>([])
const total = ref(0)
const loading = ref(false)
const route = useRoute()

const filters = reactive({
  projectId: '',
  companyId: '',
  kbId: '',
  status: '',
  answerMode: '',
  feedback: '',
  reviewStatus: '',
  q: '',
  timeRange: [] as string[],
})

const pager = reactive({
  page: 1,
  pageSize: 20,
})

const advancedOpen = ref(false)
const drawerSize = ref('780px')

const drawer = reactive({
  open: false,
  loading: false,
  detail: null as ProjectTraceDetail | null,
})

// 登记入口：project_key 必须与调用方请求头一致，创建后不可修改。
const addProject = reactive({
  open: false,
  submitting: false,
  projectKey: '',
  displayName: '',
  description: '',
  retentionDays: 90,
})

const expandedSources = reactive(new Set<string>())
const openedContexts = reactive(new Set<string>())

type PillTone = 'success' | 'warning' | 'danger' | 'info' | 'primary'

const statusMeta: Record<string, { label: string; tone: PillTone }> = {
  running: { label: '进行中', tone: 'primary' },
  success: { label: '成功', tone: 'success' },
  no_results: { label: '未命中', tone: 'warning' },
  failed: { label: '失败', tone: 'danger' },
  clarification: { label: '澄清', tone: 'primary' },
}

const modeLabel: Record<string, string> = {
  faq: 'FAQ 直出',
  direct: '知识直出',
  rag: '知识检索',
  rag_data: '检索 + 数据',
  health_data: '健康数据',
  chat: '通用对话',
  clarification: '澄清',
  refusal: '拒答',
  knowledge_unavailable: '检索不可用',
  relevance_rejected: '相关性拒绝',
  product_no_results: '产品未命中',
  knowledge_missing: '知识库缺失',
  health_database_query_failed: '健康数据失败',
  chat_processing_failed: '处理失败',
}

const modeOptions = Object.entries(modeLabel).map(([value, label]) => ({ value, label }))

const selectedProject = computed(() =>
  projects.value.find((project) => project.id === filters.projectId),
)

const retentionText = computed(() => {
  if (selectedProject.value) return `保留 ${selectedProject.value.retention_days} 天`
  const days = new Set(projects.value.map((project) => project.retention_days))
  if (projects.value.length > 0 && days.size === 1) return `保留 ${[...days][0]} 天`
  return '按项目策略保留'
})

const advancedCount = computed(
  () =>
    Number(Boolean(filters.companyId)) +
    Number(Boolean(filters.kbId)) +
    Number(Boolean(filters.answerMode)) +
    Number(Boolean(filters.feedback)) +
    Number(Boolean(filters.reviewStatus)),
)

const hasActiveFilters = computed(
  () =>
    Boolean(filters.q.trim()) ||
    Boolean(filters.status) ||
    Boolean(filters.projectId) ||
    filters.timeRange.length > 0 ||
    advancedCount.value > 0,
)

interface DayGroup {
  key: string
  label: string
  items: ProjectTraceItem[]
}

const dayGroups = computed<DayGroup[]>(() => {
  const groups: DayGroup[] = []
  const index = new Map<string, DayGroup>()
  for (const row of rows.value) {
    // 按北京时间分日，和列表展示的口径一致。
    const key = beijingDateKey(row.created_at)
    let group = index.get(key)
    if (!group) {
      group = { key, label: formatDayLabel(key), items: [] }
      index.set(key, group)
      groups.push(group)
    }
    group.items.push(row)
  }
  return groups
})

const sourceGroups = computed<SourceGroup[]>(() => {
  const detail = drawer.detail
  if (!detail) return []
  const attempts = new Map(detail.retrieval_attempts.map((item) => [item.index, item]))
  const groups = new Map<number, SourceGroup>()
  for (const source of detail.sources) {
    let group = groups.get(source.attempt_index)
    if (!group) {
      group = {
        index: source.attempt_index,
        attempt: attempts.get(source.attempt_index) ?? null,
        sources: [],
      }
      groups.set(source.attempt_index, group)
    }
    group.sources.push(source)
  }
  return [...groups.values()].sort((a, b) => a.index - b.index)
})

const prettyIntent = computed(() => {
  const intent = drawer.detail?.intent
  if (!intent || Object.keys(intent).length === 0) return ''
  return JSON.stringify(intent, null, 2)
})

function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

function formatDayLabel(key: string): string {
  const [year, month, day] = key.split('-').map(Number)
  if (!year || !month || !day) return key
  const date = new Date(year, month - 1, day)
  const weekdays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
  const monthDay = `${month}月${day}日`
  // 「今天/昨天」也按北京时间判断，避免凌晨前后归错组。
  const today = todayBeijingDateKey()
  const yesterday = beijingDateKey(new Date(Date.now() - 86_400_000).toISOString())
  if (key === today) return `今天 · ${monthDay}`
  if (key === yesterday) return `昨天 · ${monthDay}`
  if (year !== Number(today.slice(0, 4))) {
    return `${year}年${monthDay} · ${weekdays[date.getDay()]}`
  }
  return `${monthDay} · ${weekdays[date.getDay()]}`
}

function formatClock(value: string): string {
  // 库存 UTC，界面按北京时间展示。
  return formatBeijingClock(value, false)
}

function formatDateTime(value: string | null): string {
  if (!value) return '—'
  return formatBeijingDateTime(value)
}

function formatMs(value: number | null): string {
  if (value === null || value === undefined) return '—'
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(2)} s`
}

function formatCompactMs(value: number | null): string {
  if (value === null || value === undefined) return '—'
  return value < 1000 ? `${value}ms` : `${(value / 1000).toFixed(1)}s`
}

function formatScore(value: number | null | undefined, digits = 4): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

function formatTokens(value: number | null): string {
  return value === null || value === undefined ? '—' : value.toLocaleString('zh-CN')
}

function statusLabel(status: string): string {
  return statusMeta[status]?.label ?? status
}

function statusTone(status: string): PillTone {
  return statusMeta[status]?.tone ?? 'info'
}

function modeText(mode: string | null): string {
  if (!mode) return '—'
  return modeLabel[mode] ?? mode
}

// rerank 是 0~1 的置信度，分三档给快照打分条上色。
function scoreTier(score: number | null): 'high' | 'mid' | 'low' | 'none' {
  if (score === null || score === undefined) return 'none'
  if (score >= 0.7) return 'high'
  if (score >= 0.4) return 'mid'
  return 'low'
}

function scoreWidth(score: number | null): number {
  if (score === null || score === undefined) return 0
  return Math.round(Math.min(Math.max(score, 0), 1) * 100)
}

function stripMarkdown(text: string): string {
  return text
    .replace(/!\[[^\]]*\]\([^)]+\)/g, '［图片］')
    .replace(/[#>*`|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

function previewText(row: ProjectTraceItem): string {
  if (row.answer_preview) {
    const text = stripMarkdown(row.answer_preview)
    if (text) return text
  }
  if (row.status === 'running') return '等待项目回填最终回答…'
  if (row.status === 'no_results') return '本次请求未召回到相关片段'
  if (row.status === 'clarification') return '项目发起了澄清追问'
  if (row.status === 'failed') return row.error_message || '请求处理失败'
  return '暂无回答摘要'
}

function rowTags(row: ProjectTraceItem): string[] {
  const tags: string[] = []
  if (row.product_name) tags.push(row.product_name)
  if (row.model_name) tags.push(row.model_name)
  return tags
}

function canExpand(content: string): boolean {
  return content.length > 320
}

function toggleSource(id: string) {
  if (expandedSources.has(id)) expandedSources.delete(id)
  else expandedSources.add(id)
}

function toggleContext(id: string) {
  if (openedContexts.has(id)) openedContexts.delete(id)
  else openedContexts.add(id)
}

async function loadProjects() {
  try {
    const { data } = await client.get<Project[]>('/admin/projects')
    projects.value = data
  } catch (error) {
    errorMessage(error, '加载项目列表失败')
  }
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

async function loadTraces() {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: pager.page,
      page_size: pager.pageSize,
    }
    if (filters.projectId) params.project_id = filters.projectId
    if (filters.companyId) params.company_id = filters.companyId
    if (filters.kbId) params.knowledge_base_id = filters.kbId
    if (filters.status) params.status = filters.status
    if (filters.answerMode) params.answer_mode = filters.answerMode
    if (filters.feedback) params.feedback = filters.feedback
    if (filters.reviewStatus) params.review_status = filters.reviewStatus
    if (filters.q.trim()) params.q = filters.q.trim()
    if (filters.timeRange.length === 2) {
      params.start_time = filters.timeRange[0]
      params.end_time = filters.timeRange[1]
    }
    const { data } = await client.get('/admin/project-traces', { params })
    rows.value = data.items
    total.value = data.total
  } catch (error) {
    errorMessage(error, '加载项目轨迹失败')
  } finally {
    loading.value = false
  }
}

function search() {
  pager.page = 1
  loadTraces()
}

function selectProject(id: string) {
  if (filters.projectId === id) return
  filters.projectId = id
  search()
}

function openAddProject() {
  addProject.projectKey = ''
  addProject.displayName = ''
  addProject.description = ''
  addProject.retentionDays = 90
  addProject.submitting = false
  addProject.open = true
}

async function submitAddProject() {
  const projectKey = addProject.projectKey.trim()
  const displayName = addProject.displayName.trim()
  if (!projectKey || !displayName) {
    ElMessage.warning('请填写项目标识和展示名称')
    return
  }
  addProject.submitting = true
  try {
    const { data } = await client.post<Project>('/admin/projects', {
      project_key: projectKey,
      display_name: displayName,
      description: addProject.description.trim() || null,
      retention_days: addProject.retentionDays,
    })
    addProject.open = false
    await loadProjects()
    filters.projectId = data.id
    search()
    ElMessage.success('项目已登记，接入后的新请求会出现在这里')
  } catch (error) {
    errorMessage(error, '登记项目失败')
  } finally {
    addProject.submitting = false
  }
}

function resetAdvanced() {
  filters.companyId = ''
  filters.kbId = ''
  filters.answerMode = ''
  filters.feedback = ''
  filters.reviewStatus = ''
  kbs.value = []
}

function applyAdvanced() {
  advancedOpen.value = false
  search()
}

function resetFilters() {
  filters.projectId = ''
  resetAdvanced()
  filters.status = ''
  filters.q = ''
  filters.timeRange = []
  search()
}

async function reloadAll() {
  await Promise.all([loadProjects(), loadTraces()])
}

function drawerSizeForViewport(): string {
  return `${Math.min(780, window.innerWidth)}px`
}

async function openDetail(row: ProjectTraceItem) {
  drawer.open = true
  drawer.loading = true
  drawer.detail = null
  drawerSize.value = drawerSizeForViewport()
  expandedSources.clear()
  openedContexts.clear()
  try {
    const { data } = await client.get<ProjectTraceDetail>(`/admin/project-traces/${row.id}`)
    // 回答与快照中的图片需要带鉴权拉取，转成 blob URL 后再渲染。
    const [answer, sources] = await Promise.all([
      data.answer ? resolveInlineImages(data.answer) : Promise.resolve(data.answer),
      Promise.all(
        data.sources.map(async (source) => ({
          ...source,
          content_snapshot: await resolveInlineImages(source.content_snapshot),
          context_snapshot: source.context_snapshot
            ? await resolveInlineImages(source.context_snapshot)
            : source.context_snapshot,
        })),
      ),
    ])
    drawer.detail = { ...data, answer, sources }
  } catch (error) {
    errorMessage(error, '加载轨迹详情失败')
    drawer.open = false
  } finally {
    drawer.loading = false
  }
}

watch(
  () => filters.companyId,
  async () => {
    filters.kbId = ''
    await loadKbs()
  },
)

// 外部项目回传点踩后进入待补资料，这里由运营确认已补充并关闭。
async function toggleTraceReview() {
  const detail = drawer.detail
  if (!detail) return
  const target = detail.review_status === 'resolved' ? 'none' : 'resolved'
  try {
    const { data } = await client.patch<ProjectTraceItem>(
      `/admin/project-traces/${detail.id}/review`,
      { review_status: target },
    )
    detail.review_status = data.review_status
    const row = rows.value.find((item) => item.id === detail.id)
    if (row) row.review_status = data.review_status
    ElMessage.success(target === 'resolved' ? '已标记为已处理' : '已重新打开复核')
  } catch (error) {
    errorMessage(error, '更新复核状态失败')
  }
}

onMounted(async () => {
  // 全局搜索跳转时带上关键词，进入即按问题文本过滤。
  if (typeof route.query.q === 'string') filters.q = route.query.q
  await Promise.all([loadProjects(), loadCompanies()])
  await loadTraces()
})
</script>

<template>
  <div class="trace-page">
    <PageHeader
      title="项目问答轨迹"
      eyebrow="PROJECT TRACE"
      subtitle="外部项目每一次提问、召回片段与最终回答，都按请求留档。"
    >
      <template #meta>
        <span class="header-count">{{ total.toLocaleString('zh-CN') }} 条轨迹</span>
      </template>
      <template #actions>
        <span class="retention-pill">
          <el-icon><Clock /></el-icon>
          {{ retentionText }}
        </span>
        <el-button v-if="canWrite" size="small" @click="openAddProject">
          <el-icon><Plus /></el-icon>添加项目
        </el-button>
        <button class="refresh-btn" type="button" title="刷新" @click="reloadAll">
          <el-icon :class="{ spinning: loading }"><Refresh /></el-icon>
        </button>
      </template>
    </PageHeader>

    <section class="project-strip">
      <div class="project-chips">
        <button
          class="project-chip"
          :class="{ active: !filters.projectId }"
          type="button"
          @click="selectProject('')"
        >
          全部项目
        </button>
        <button
          v-for="project in projects"
          :key="project.id"
          class="project-chip"
          :class="{ active: filters.projectId === project.id }"
          type="button"
          @click="selectProject(project.id)"
        >
          <i class="chip-dot" :class="{ off: !project.is_enabled }" />
          {{ project.display_name }}
          <em>{{ project.project_key }}</em>
        </button>
      </div>
      <span class="strip-note"><el-icon><Lock /></el-icon>仅记录接入后的新请求</span>
    </section>

    <DataPanel flush>
      <div class="panel-toolbar">
        <FilterBar>
          <el-select v-model="filters.status" clearable placeholder="全部状态" class="status-select">
            <el-option label="成功" value="success" />
            <el-option label="未命中" value="no_results" />
            <el-option label="失败" value="failed" />
            <el-option label="澄清" value="clarification" />
            <el-option label="进行中" value="running" />
          </el-select>

          <el-input
            v-model="filters.q"
            clearable
            placeholder="搜索问题关键词"
            class="q-input"
            @keyup.enter="search"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>

          <el-date-picker
            v-model="filters.timeRange"
            type="datetimerange"
            range-separator="至"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
            value-format="YYYY-MM-DDTHH:mm:ss"
            class="time-picker"
          />

          <el-popover v-model:visible="advancedOpen" placement="bottom-end" :width="360" trigger="click">
            <template #reference>
              <el-button class="advanced-btn">
                <el-icon><Filter /></el-icon>
                高级筛选
                <span v-if="advancedCount" class="advanced-count">{{ advancedCount }}</span>
              </el-button>
            </template>
            <div class="adv-panel">
              <div class="adv-head">
                <span>高级筛选</span>
                <button type="button" @click="resetAdvanced">清空条件</button>
              </div>
              <label class="adv-field">
                <span>公司</span>
                <el-select v-model="filters.companyId" clearable placeholder="全部公司">
                  <el-option
                    v-for="company in companies"
                    :key="company.id"
                    :label="company.name"
                    :value="company.id"
                  />
                </el-select>
              </label>
              <label class="adv-field">
                <span>知识库</span>
                <el-select
                  v-model="filters.kbId"
                  clearable
                  placeholder="全部知识库"
                  :disabled="!filters.companyId"
                >
                  <el-option v-for="kb in kbs" :key="kb.id" :label="kb.name" :value="kb.id" />
                </el-select>
              </label>
              <label class="adv-field">
                <span>回答路径</span>
                <el-select v-model="filters.answerMode" clearable filterable placeholder="全部路径">
                  <el-option
                    v-for="mode in modeOptions"
                    :key="mode.value"
                    :label="mode.label"
                    :value="mode.value"
                  />
                </el-select>
              </label>
              <label class="adv-field">
                <span>用户反馈</span>
                <el-select v-model="filters.feedback" clearable placeholder="全部反馈">
                  <el-option label="有用" value="up" />
                  <el-option label="没用" value="down" />
                </el-select>
              </label>
              <label class="adv-field">
                <span>复核状态</span>
                <el-select v-model="filters.reviewStatus" clearable placeholder="全部复核状态">
                  <el-option label="待补资料" value="pending" />
                  <el-option label="已补充" value="resolved" />
                  <el-option label="未复核" value="none" />
                </el-select>
              </label>
              <div class="adv-actions">
                <el-button size="small" @click="resetAdvanced">清空</el-button>
                <el-button size="small" type="primary" @click="applyAdvanced">应用筛选</el-button>
              </div>
            </div>
          </el-popover>

          <template #actions>
            <el-button type="primary" @click="search">查询</el-button>
            <el-button @click="resetFilters">重置</el-button>
          </template>
        </FilterBar>
      </div>

      <div v-loading="loading" class="ledger-body">
        <template v-if="dayGroups.length">
          <div v-for="(group, groupIndex) in dayGroups" :key="group.key" class="day-group">
            <div class="day-head">
              <span class="day-label">{{ group.label }}</span>
              <span class="day-count">{{ group.items.length }} 条</span>
              <i class="day-line" />
            </div>

            <button
              v-for="(row, rowIndex) in group.items"
              :key="row.id"
              class="trace-row"
              :class="{ 'is-live': row.status === 'running' }"
              :style="{ animationDelay: `${Math.min(groupIndex * 40 + rowIndex * 24, 320)}ms` }"
              type="button"
              @click="openDetail(row)"
            >
              <div class="time-rail">
                <span class="time-text">{{ formatClock(row.created_at) }}</span>
                <span class="rail-line" />
              </div>

              <div class="row-main">
                <div class="query-line">
                  <span class="query-text">{{ row.query }}</span>
                  <StatusPill
                    :label="statusLabel(row.status)"
                    :tone="statusTone(row.status)"
                    :pulse="row.status === 'running'"
                  />
                </div>
                <p class="answer-preview">{{ previewText(row) }}</p>
                <div class="row-tags">
                  <span class="tag-project">{{ row.project_name }}</span>
                  <span class="tag-plain">{{ row.company_name ?? '未知公司' }}</span>
                  <span class="tag-plain">{{ row.knowledge_base_name }}</span>
                  <span v-for="tag in rowTags(row)" :key="tag" class="tag-muted">{{ tag }}</span>
                  <span class="mode-chip">{{ modeText(row.answer_mode) }}</span>
                  <StatusPill v-if="row.feedback === 'up'" label="有用" tone="success" :dot="false" />
                  <StatusPill v-else-if="row.feedback === 'down'" label="没用" tone="danger" :dot="false" />
                  <StatusPill
                    v-if="row.review_status === 'pending'"
                    label="待补资料"
                    tone="warning"
                    :dot="false"
                  />
                  <StatusPill
                    v-else-if="row.review_status === 'resolved'"
                    label="已处理"
                    tone="info"
                    :dot="false"
                  />
                </div>
              </div>

              <div class="row-stats">
                <div class="stat">
                  <strong>{{ row.source_count }}</strong>
                  <span>召回</span>
                </div>
                <div class="stat">
                  <strong class="mono">{{ formatScore(row.top_rerank_score, 3) }}</strong>
                  <span>最高分</span>
                </div>
                <div class="stat">
                  <strong class="mono">{{ formatCompactMs(row.total_duration_ms) }}</strong>
                  <span>耗时</span>
                </div>
              </div>

              <el-icon class="row-arrow"><ArrowRight /></el-icon>
            </button>
          </div>
        </template>

        <EmptyState
          v-else-if="!loading"
          :title="hasActiveFilters ? '当前条件下没有记录' : '还没有项目轨迹'"
          :text="
            hasActiveFilters
              ? '换个项目或放宽筛选条件再试。'
              : '接入项目完成一次问答后，检索依据与回答会出现在这里。'
          "
          :muted="hasActiveFilters"
        >
          <el-button v-if="hasActiveFilters" @click="resetFilters">清空筛选</el-button>
        </EmptyState>
      </div>

      <template v-if="dayGroups.length" #footer>
        <el-pagination
          v-model:current-page="pager.page"
          v-model:page-size="pager.pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          background
          layout="total, sizes, prev, pager, next"
          @current-change="loadTraces"
          @size-change="search"
        />
      </template>
    </DataPanel>

    <el-drawer
      v-model="drawer.open"
      :size="drawerSize"
      :with-header="false"
      destroy-on-close
      class="trace-drawer"
    >
      <div v-loading="drawer.loading" class="drawer-shell">
        <template v-if="drawer.detail">
          <header class="detail-head">
            <div class="detail-eyebrow">
              <span>{{ drawer.detail.project_name }}</span>
              <em>{{ drawer.detail.project_key }}</em>
              <StatusPill
                :label="statusLabel(drawer.detail.status)"
                :tone="statusTone(drawer.detail.status)"
                :pulse="drawer.detail.status === 'running'"
              />
            </div>
            <h2 class="detail-query">{{ drawer.detail.query }}</h2>
            <div class="detail-meta">
              <span>{{ formatDateTime(drawer.detail.created_at) }}</span>
              <i class="meta-sep" />
              <span>{{ drawer.detail.company_name ?? '未知公司' }}</span>
              <i class="meta-sep" />
              <span>{{ drawer.detail.knowledge_base_name }}</span>
              <span class="mode-chip">{{ modeText(drawer.detail.answer_mode) }}</span>
              <button
                v-if="canWrite && drawer.detail.review_status !== 'resolved'"
                class="review-action"
                type="button"
                @click.stop="toggleTraceReview"
              >
                标记已处理
              </button>
              <button
                v-else-if="canWrite"
                class="review-action is-done"
                type="button"
                @click.stop="toggleTraceReview"
              >
                已处理 · 重新打开
              </button>
            </div>
            <button class="drawer-close" type="button" title="关闭" @click="drawer.open = false">
              <el-icon><Close /></el-icon>
            </button>
          </header>

          <el-alert
            v-if="drawer.detail.error_message"
            class="error-alert"
            type="error"
            :closable="false"
            show-icon
            :title="drawer.detail.error_code || '请求失败'"
            :description="drawer.detail.error_message"
          />

          <section class="detail-section">
            <div class="section-head">
              <span class="section-no">01</span>
              <h3>最终回答</h3>
              <span v-if="drawer.detail.chat_model_name" class="section-aside">
                {{ drawer.detail.chat_model_name }}
              </span>
            </div>
            <div
              v-if="drawer.detail.answer"
              class="answer-panel markdown-body"
              v-html="renderMarkdown(drawer.detail.answer)"
            />
            <p v-else-if="drawer.detail.status === 'running'" class="muted-note">
              请求仍在处理，等待项目侧回填最终回答。
            </p>
            <p v-else class="muted-note">该请求没有生成回答。</p>
          </section>

          <section class="detail-section">
            <div class="section-head">
              <span class="section-no">02</span>
              <h3>运行概览</h3>
            </div>
            <div class="stat-strip">
              <div class="stat-cell">
                <strong>{{ drawer.detail.retrieval_attempts.length }}</strong>
                <span>检索尝试</span>
              </div>
              <div class="stat-cell">
                <strong>{{ drawer.detail.source_count }}</strong>
                <span>召回片段</span>
              </div>
              <div class="stat-cell">
                <strong class="mono">{{ formatScore(drawer.detail.top_rerank_score, 4) }}</strong>
                <span>最高重排分</span>
              </div>
              <div class="stat-cell">
                <strong class="mono">{{ formatCompactMs(drawer.detail.retrieval_duration_ms) }}</strong>
                <span>检索耗时</span>
              </div>
              <div class="stat-cell">
                <strong class="mono">{{ formatCompactMs(drawer.detail.generation_duration_ms) }}</strong>
                <span>生成耗时</span>
              </div>
              <div class="stat-cell">
                <strong class="mono">{{ formatCompactMs(drawer.detail.total_duration_ms) }}</strong>
                <span>总耗时</span>
              </div>
            </div>

            <div class="meta-grid">
              <div class="meta-item">
                <span class="meta-label">检索模式</span>
                <span>{{ drawer.detail.retrieval_mode ?? '—' }}</span>
              </div>
              <div class="meta-item">
                <span class="meta-label">Top K</span>
                <span>{{ drawer.detail.top_k ?? '—' }}</span>
              </div>
              <div class="meta-item">
                <span class="meta-label">上下文扩展</span>
                <span>{{ drawer.detail.include_context ? '开启' : '关闭' }}</span>
              </div>
              <div class="meta-item">
                <span class="meta-label">回答路径</span>
                <span>{{ modeText(drawer.detail.answer_mode) }}</span>
              </div>
              <div class="meta-item">
                <span class="meta-label">产品过滤</span>
                <span>{{ drawer.detail.product_name ?? '—' }}</span>
              </div>
              <div class="meta-item">
                <span class="meta-label">型号过滤</span>
                <span>{{ drawer.detail.model_name ?? '—' }}</span>
              </div>
              <div class="meta-item">
                <span class="meta-label">用户反馈</span>
                <span>
                  {{ drawer.detail.feedback === 'up' ? '有用' : drawer.detail.feedback === 'down' ? '没用' : '—' }}
                  <em v-if="drawer.detail.feedback_at" class="meta-sub">
                    {{ formatDateTime(drawer.detail.feedback_at) }}
                  </em>
                </span>
              </div>
              <div class="meta-item">
                <span class="meta-label">复核状态</span>
                <span>
                  {{ drawer.detail.review_status === 'pending' ? '待补资料' : drawer.detail.review_status === 'resolved' ? '已处理' : '未复核' }}
                </span>
              </div>
              <div v-if="drawer.detail.feedback_reason" class="meta-item meta-wide">
                <span class="meta-label">反馈原因</span>
                <span>{{ drawer.detail.feedback_reason }}</span>
              </div>
              <div class="meta-item">
                <span class="meta-label">对话模型</span>
                <span>
                  {{ drawer.detail.chat_provider ? `${drawer.detail.chat_provider} · ` : ''
                  }}{{ drawer.detail.chat_model_name ?? '—' }}
                </span>
              </div>
              <div class="meta-item">
                <span class="meta-label">Token</span>
                <span>
                  共 {{ formatTokens(drawer.detail.total_tokens) }}
                  <em class="meta-sub">
                    输入 {{ formatTokens(drawer.detail.prompt_tokens) }} / 输出
                    {{ formatTokens(drawer.detail.completion_tokens) }}
                  </em>
                </span>
              </div>
              <div class="meta-item">
                <span class="meta-label">创建时间</span>
                <span>{{ formatDateTime(drawer.detail.created_at) }}</span>
              </div>
              <div class="meta-item">
                <span class="meta-label">完成时间</span>
                <span>{{ formatDateTime(drawer.detail.finished_at) }}</span>
              </div>
            </div>
          </section>

          <section class="detail-section">
            <div class="section-head">
              <span class="section-no">03</span>
              <h3>检索尝试</h3>
              <span class="section-aside">{{ drawer.detail.retrieval_attempts.length }} 次</span>
            </div>
            <div v-if="drawer.detail.retrieval_attempts.length" class="attempt-list">
              <div
                v-for="attempt in drawer.detail.retrieval_attempts"
                :key="attempt.index"
                class="attempt-row"
                :class="{ 'has-error': attempt.error_code }"
              >
                <span class="attempt-index">{{ pad2(attempt.index + 1) }}</span>
                <div class="attempt-main">
                  <div class="attempt-query">{{ attempt.query }}</div>
                  <div class="attempt-meta">
                    <span>{{ attempt.retrieval_mode }}</span>
                    <i />
                    <span>Top {{ attempt.top_k }}</span>
                    <i />
                    <span>召回 {{ attempt.result_count }}</span>
                    <i />
                    <span>{{ formatMs(attempt.duration_ms) }}</span>
                    <template v-if="attempt.product_name || attempt.model_name">
                      <i />
                      <span>{{ [attempt.product_name, attempt.model_name].filter(Boolean).join(' / ') }}</span>
                    </template>
                  </div>
                  <div v-if="attempt.error_code" class="attempt-error">
                    {{ attempt.error_code }}<template v-if="attempt.error_message">：{{ attempt.error_message }}</template>
                  </div>
                </div>
                <span class="attempt-score mono">{{ formatScore(attempt.top_score, 3) }}</span>
              </div>
            </div>
            <p v-else class="muted-note">该项目未经过知识库检索，或检索信息未上报。</p>
          </section>

          <section class="detail-section">
            <div class="section-head">
              <span class="section-no">04</span>
              <h3>召回片段</h3>
              <span class="section-aside">{{ drawer.detail.sources.length }} 条快照</span>
            </div>
            <div v-if="sourceGroups.length" class="source-groups">
              <div v-for="group in sourceGroups" :key="group.index" class="source-group">
                <div class="group-head">
                  <span class="group-index">#{{ pad2(group.index + 1) }}</span>
                  <span class="group-title">第 {{ group.index + 1 }} 次检索</span>
                  <span class="group-meta">{{ group.sources.length }} 条</span>
                  <span v-if="group.attempt" class="group-meta">
                    {{ group.attempt.retrieval_mode }} · Top {{ group.attempt.top_k }} ·
                    {{ formatMs(group.attempt.duration_ms) }}
                  </span>
                </div>

                <article v-for="source in group.sources" :key="source.id" class="source-card">
                  <header class="source-head">
                    <span class="source-rank">{{ source.rank }}</span>
                    <span class="source-file">{{ source.filename }}<em>v{{ source.version_number }}</em></span>
                    <span v-if="source.page_number" class="source-chip">第 {{ source.page_number }} 页</span>
                    <span v-if="source.section" class="source-chip">{{ source.section }}</span>
                  </header>

                  <div class="score-line">
                    <template v-if="source.rerank_score !== null">
                      <span class="score-key">rerank</span>
                      <span class="score-bar">
                        <i
                          :class="['score-fill', scoreTier(source.rerank_score)]"
                          :style="{ width: `${scoreWidth(source.rerank_score)}%` }"
                        />
                      </span>
                      <strong>{{ formatScore(source.rerank_score) }}</strong>
                    </template>
                    <span class="score-pair"><em>bm25</em>{{ formatScore(source.bm25_score) }}</span>
                    <span class="score-pair"><em>rrf</em>{{ formatScore(source.rrf_score) }}</span>
                    <span class="score-pair"><em>cosine</em>{{ formatScore(source.cosine_distance) }}</span>
                  </div>

                  <div
                    class="snapshot markdown-body"
                    :class="{ clamped: canExpand(source.content_snapshot) && !expandedSources.has(source.id) }"
                    v-html="renderMarkdown(source.content_snapshot)"
                  />

                  <div class="source-actions">
                    <button
                      v-if="canExpand(source.content_snapshot)"
                      type="button"
                      @click="toggleSource(source.id)"
                    >
                      {{ expandedSources.has(source.id) ? '收起片段' : '展开全文' }}
                      <el-icon><component :is="expandedSources.has(source.id) ? 'ArrowUp' : 'ArrowDown'" /></el-icon>
                    </button>
                    <button
                      v-if="source.context_snapshot"
                      type="button"
                      @click="toggleContext(source.id)"
                    >
                      {{ openedContexts.has(source.id) ? '收起上下文' : '查看上下文' }}
                      <el-icon><component :is="openedContexts.has(source.id) ? 'ArrowUp' : 'ArrowDown'" /></el-icon>
                    </button>
                  </div>

                  <div
                    v-if="openedContexts.has(source.id) && source.context_snapshot"
                    class="context-panel markdown-body"
                    v-html="renderMarkdown(source.context_snapshot)"
                  />
                </article>
              </div>
            </div>
            <p v-else class="muted-note">没有保存召回片段；可能是 FAQ 直出、澄清或回答路径未触发检索。</p>
          </section>

          <section v-if="prettyIntent" class="detail-section">
            <div class="section-head">
              <span class="section-no">05</span>
              <h3>意图识别</h3>
            </div>
            <pre class="code-block">{{ prettyIntent }}</pre>
          </section>

          <section class="detail-section">
            <div class="section-head">
              <span class="section-no">06</span>
              <h3>请求标识</h3>
            </div>
            <div class="ident-grid">
              <div class="ident-item">
                <span>请求 ID</span>
                <code>{{ drawer.detail.external_request_id }}</code>
              </div>
              <div class="ident-item">
                <span>轨迹 ID</span>
                <code>{{ drawer.detail.id }}</code>
              </div>
              <div class="ident-item">
                <span>会话标识</span>
                <code>{{ drawer.detail.session_ref || '—' }}</code>
              </div>
              <div class="ident-item">
                <span>用户标识</span>
                <code>{{ drawer.detail.user_ref || '—' }}</code>
              </div>
            </div>
          </section>
        </template>
      </div>
    </el-drawer>

    <el-dialog
      v-model="addProject.open"
      title="登记接入项目"
      width="460px"
      :close-on-click-modal="false"
    >
      <p class="add-hint">
        项目标识需与调用方请求头 <code>X-Project-Key</code> 完全一致；登记后只记录新请求。
      </p>
      <el-form label-width="88px">
        <el-form-item label="项目标识" required>
          <el-input
            v-model="addProject.projectKey"
            maxlength="80"
            placeholder="例如 health_agent"
            :disabled="addProject.submitting"
          />
        </el-form-item>
        <el-form-item label="展示名称" required>
          <el-input
            v-model="addProject.displayName"
            maxlength="160"
            placeholder="例如 健康管家"
            :disabled="addProject.submitting"
          />
        </el-form-item>
        <el-form-item label="项目说明">
          <el-input
            v-model="addProject.description"
            type="textarea"
            :rows="2"
            maxlength="1000"
            placeholder="可选"
            :disabled="addProject.submitting"
          />
        </el-form-item>
        <el-form-item label="保留天数">
          <el-input-number
            v-model="addProject.retentionDays"
            :min="1"
            :max="3650"
            :step="30"
            controls-position="right"
            :disabled="addProject.submitting"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addProject.open = false">取消</el-button>
        <el-button type="primary" :loading="addProject.submitting" @click="submitAddProject">
          登记项目
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.header-count {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}
/* 与 header 里的小号按钮同高，保持动作区一行对齐。 */
.retention-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 24px;
  padding: 0 9px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 999px;
  background: #fff;
  font-size: var(--kb-font-2xs);
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}
.retention-pill .el-icon {
  color: var(--el-color-primary);
}
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
  transition: color 0.2s var(--kb-ease), border-color 0.2s var(--kb-ease),
    background-color 0.2s var(--kb-ease);
}
.refresh-btn:hover {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary-light-7);
  background: #fff;
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
.project-strip {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 14px;
  padding: 0 2px;
}
.project-chips {
  display: flex;
  align-items: center;
  gap: 8px;
  overflow-x: auto;
  scrollbar-width: none;
}
.project-chips::-webkit-scrollbar {
  display: none;
}
.project-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 999px;
  background: #fff;
  color: var(--el-text-color-regular);
  font-size: 12px;
  font-family: inherit;
  white-space: nowrap;
  cursor: pointer;
  transition: border-color 0.18s var(--kb-ease), background-color 0.18s var(--kb-ease),
    color 0.18s var(--kb-ease);
}
.project-chip:hover {
  border-color: var(--el-color-primary-light-7);
  color: var(--el-color-primary);
}
.project-chip.active {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary);
  color: #fff;
}
.project-chip em {
  font-style: normal;
  font-size: 10px;
  font-family: 'Cascadia Mono', Consolas, monospace;
  color: var(--el-text-color-secondary);
  letter-spacing: 0.02em;
}
.project-chip.active em {
  color: rgba(255, 255, 255, 0.68);
}
.chip-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--el-color-success);
}
.chip-dot.off {
  background: var(--el-text-color-disabled);
}
.project-chip.active .chip-dot {
  background: #fff;
}
.add-hint {
  margin: 0 0 16px;
  padding: 9px 11px;
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
.add-hint code {
  padding: 1px 5px;
  border-radius: 4px;
  background: #fff;
  border: 1px solid var(--el-border-color-lighter);
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 11px;
  color: var(--el-color-primary-dark-2);
}
.strip-note {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex: none;
  font-size: 11px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}
.strip-note .el-icon {
  font-size: 12px;
}

/* —— 轨迹台账 —— */
.panel-toolbar {
  padding: 14px 18px 0;
}
.status-select {
  width: 126px;
  flex: none;
}
.q-input {
  width: 210px;
  flex: none;
}
.time-picker {
  width: 330px;
  flex: none;
}
.advanced-btn {
  flex: none;
}
.advanced-btn .el-icon {
  margin-right: 4px;
}
.advanced-count {
  display: inline-grid;
  place-items: center;
  min-width: 16px;
  height: 16px;
  margin-left: 4px;
  padding: 0 4px;
  border-radius: 999px;
  background: var(--el-color-primary);
  color: #fff;
  font-size: 10px;
}
.ledger-body {
  min-height: 320px;
  padding: 6px 10px 12px;
}
.day-group {
  padding-top: 4px;
}
.day-head {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 12px 10px 6px;
}
.day-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-regular);
  white-space: nowrap;
}
.day-count {
  font-size: 11px;
  color: var(--el-text-color-placeholder);
  white-space: nowrap;
}
.day-line {
  flex: 1;
  height: 1px;
  background: linear-gradient(90deg, var(--el-border-color-lighter), transparent);
}
.trace-row {
  display: grid;
  grid-template-columns: 50px minmax(0, 1fr) 224px 18px;
  align-items: center;
  gap: 16px;
  width: 100%;
  min-height: 86px;
  padding: 12px 14px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  animation: row-in 0.3s var(--kb-ease) both;
  transition: background-color 0.18s var(--kb-ease), border-color 0.18s var(--kb-ease);
}
@keyframes row-in {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.trace-row:hover {
  background: #f8f9ff;
  border-color: #e9e9fb;
}
.trace-row.is-live .rail-line {
  background: linear-gradient(180deg, var(--el-color-primary-light-5), transparent);
}
.time-rail {
  align-self: stretch;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 7px;
  padding-top: 3px;
}
.time-text {
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  letter-spacing: 0.02em;
}
.rail-line {
  width: 18px;
  height: 2px;
  border-radius: 999px;
  background: var(--el-border-color);
}
.row-main {
  min-width: 0;
}
.query-line {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.query-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.answer-preview {
  margin: 5px 0 7px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}
.row-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
}
.tag-project {
  display: inline-flex;
  align-items: center;
  height: 19px;
  padding: 0 7px;
  border-radius: 5px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary-dark-2);
  font-size: 11px;
  font-weight: 500;
  flex: none;
}
.tag-plain {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 170px;
  flex: none;
}
.tag-plain::before {
  content: '·';
  margin-right: 6px;
  color: var(--el-text-color-placeholder);
}
.tag-plain:first-of-type::before {
  display: none;
}
.tag-muted {
  display: inline-flex;
  height: 19px;
  align-items: center;
  padding: 0 7px;
  border-radius: 5px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
  font-size: 11px;
  flex: none;
}
.mode-chip {
  display: inline-flex;
  align-items: center;
  height: 19px;
  padding: 0 7px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 5px;
  color: var(--el-text-color-secondary);
  font-size: 10.5px;
  letter-spacing: 0.01em;
  white-space: nowrap;
  flex: none;
  margin-left: auto;
}
.review-action {
  height: 22px;
  padding: 0 9px;
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: 999px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-size: 11px;
  font-family: inherit;
  cursor: pointer;
  transition: background-color 0.15s var(--kb-ease), transform 0.12s var(--kb-ease);
}
.review-action:hover {
  background: var(--el-color-primary-light-8);
}
.review-action:active {
  transform: scale(0.97);
}
.review-action.is-done {
  border-color: var(--el-border-color);
  background: transparent;
  color: var(--el-text-color-secondary);
}
.row-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 6px;
  padding-left: 14px;
  border-left: 1px solid var(--el-border-color-lighter);
}
.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.stat strong {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.stat span {
  font-size: 10.5px;
  color: var(--el-text-color-placeholder);
  letter-spacing: 0.02em;
}
.mono {
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-variant-numeric: tabular-nums;
}
.row-arrow {
  font-size: 14px;
  color: var(--el-text-color-placeholder);
  transition: color 0.18s var(--kb-ease), transform 0.18s var(--kb-ease);
}
.trace-row:hover .row-arrow {
  color: var(--el-color-primary);
  transform: translateX(2px);
}
/* —— 高级筛选 —— */
.adv-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.adv-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 600;
}
.adv-head button {
  border: none;
  background: none;
  padding: 0;
  font-size: 12px;
  color: var(--el-color-primary);
  cursor: pointer;
  font-family: inherit;
}
.adv-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.adv-field > span {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.adv-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 2px;
  border-top: 1px solid var(--el-border-color-lighter);
}

/* —— 详情抽屉 —— */
.drawer-shell {
  min-height: 320px;
}
.detail-head {
  position: relative;
  padding-right: 40px;
}
.detail-eyebrow {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.detail-eyebrow em {
  font-style: normal;
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 10.5px;
  color: var(--el-text-color-placeholder);
}
.detail-query {
  margin: 8px 0 8px;
  font-size: 19px;
  line-height: 1.5;
  font-weight: 650;
  letter-spacing: -0.01em;
  color: var(--el-text-color-primary);
  word-break: break-word;
}
.detail-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.meta-sep {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--el-text-color-disabled);
}
.detail-meta .mode-chip {
  margin-left: 0;
}
.drawer-close {
  position: absolute;
  top: 0;
  right: 0;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: #fff;
  color: var(--el-text-color-secondary);
  cursor: pointer;
  transition: color 0.18s var(--kb-ease), border-color 0.18s var(--kb-ease);
}
.drawer-close:hover {
  color: var(--el-text-color-primary);
  border-color: var(--el-border-color-dark);
}
.error-alert {
  margin-top: 16px;
}
.detail-section {
  margin-top: 26px;
  padding-top: 20px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.section-head {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 12px;
}
.section-no {
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 10px;
  color: var(--el-color-primary);
  letter-spacing: 0.08em;
}
.section-head h3 {
  margin: 0;
  font-size: 13px;
  font-weight: 650;
  letter-spacing: 0.02em;
  color: var(--el-text-color-primary);
}
.section-aside {
  margin-left: auto;
  font-size: 11px;
  color: var(--el-text-color-placeholder);
  white-space: nowrap;
}
.answer-panel {
  padding: 16px 18px;
  border: 1px solid var(--el-border-color-lighter);
  border-left: 3px solid var(--el-color-primary-light-5);
  border-radius: 10px;
  background: linear-gradient(180deg, #fbfbfe 0%, #fff 100%);
  font-size: 13.5px;
  line-height: 1.8;
  color: var(--el-text-color-regular);
  overflow-x: auto;
}
.muted-note {
  margin: 0;
  padding: 12px 14px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}
.stat-strip {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  overflow: hidden;
}
.stat-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 11px 12px;
  border-right: 1px solid var(--el-border-color-lighter);
  min-width: 0;
}
.stat-cell:last-child {
  border-right: none;
}
.stat-cell strong {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.stat-cell span {
  font-size: 10.5px;
  color: var(--el-text-color-placeholder);
  white-space: nowrap;
}
.meta-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 9px 22px;
  margin-top: 14px;
}
.meta-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  font-size: 12.5px;
  color: var(--el-text-color-regular);
}
.meta-item > span:last-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.meta-label {
  flex: none;
  width: 72px;
  color: var(--el-text-color-secondary);
  font-size: 11.5px;
}
.meta-sub {
  font-style: normal;
  color: var(--el-text-color-placeholder);
  font-size: 11px;
  margin-left: 4px;
}
.attempt-list {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  overflow: hidden;
}
.attempt-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  background: #fff;
}
.attempt-row:last-child {
  border-bottom: none;
}
.attempt-row.has-error {
  background: rgba(220, 38, 38, 0.025);
}
.attempt-index {
  flex: none;
  width: 26px;
  height: 20px;
  display: grid;
  place-items: center;
  border-radius: 5px;
  background: var(--el-fill-color-light);
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 10.5px;
  color: var(--el-text-color-secondary);
}
.attempt-main {
  flex: 1;
  min-width: 0;
}
.attempt-query {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--el-text-color-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.attempt-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 4px;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.attempt-meta i {
  width: 2px;
  height: 2px;
  border-radius: 50%;
  background: var(--el-text-color-disabled);
}
.attempt-error {
  margin-top: 5px;
  font-size: 11px;
  color: var(--el-color-danger);
}
.attempt-score {
  flex: none;
  font-size: 12px;
  color: var(--el-text-color-regular);
  padding-top: 2px;
}
.source-groups {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.group-index {
  font-family: 'Cascadia Mono', Consolas, monospace;
  color: var(--el-color-primary);
  font-size: 10.5px;
}
.group-title {
  font-weight: 600;
  color: var(--el-text-color-regular);
  font-size: 11.5px;
}
.group-meta {
  position: relative;
  padding-left: 9px;
}
.group-meta::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  width: 2px;
  height: 2px;
  border-radius: 50%;
  background: var(--el-text-color-disabled);
  transform: translateY(-50%);
}
.source-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  padding: 12px 14px;
  margin-bottom: 8px;
  background: #fff;
}
.source-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.source-rank {
  width: 22px;
  height: 22px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: 6px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary-dark-2);
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 11px;
  font-weight: 600;
}
.source-file {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.source-file em {
  font-style: normal;
  margin-left: 5px;
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 10px;
  font-weight: 400;
  color: var(--el-text-color-placeholder);
}
.source-chip {
  padding: 1px 7px;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  font-size: 10.5px;
  color: var(--el-text-color-secondary);
}
.score-line {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 9px;
  font-size: 10.5px;
  color: var(--el-text-color-secondary);
}
.score-key {
  flex: none;
}
.score-bar {
  width: 80px;
  height: 4px;
  flex: none;
  border-radius: 999px;
  background: var(--el-fill-color);
  overflow: hidden;
}
.score-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
}
.score-fill.high {
  background: var(--el-color-primary);
}
.score-fill.mid {
  background: var(--el-color-warning);
}
.score-fill.low {
  background: var(--el-color-danger);
}
.score-line strong {
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 11px;
  font-weight: 500;
  color: var(--el-text-color-regular);
}
.score-pair {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding-left: 10px;
  border-left: 1px solid var(--el-border-color-lighter);
}
.score-pair em {
  font-style: normal;
  color: var(--el-text-color-placeholder);
}
.score-pair {
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 10.5px;
  color: var(--el-text-color-regular);
}
.snapshot {
  margin-top: 9px;
  padding: 10px 12px;
  border-radius: 7px;
  background: var(--el-fill-color-lighter);
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--el-text-color-regular);
  overflow-x: auto;
}
.snapshot.clamped {
  max-height: 180px;
  overflow-y: hidden;
  mask-image: linear-gradient(180deg, #000 72%, transparent);
}
.source-actions {
  display: flex;
  gap: 14px;
  margin-top: 7px;
}
.source-actions button {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 0;
  border: none;
  background: none;
  font-family: inherit;
  font-size: 11px;
  color: var(--el-color-primary);
  cursor: pointer;
}
.source-actions button:hover {
  color: var(--el-color-primary-dark-2);
}
.source-actions .el-icon {
  font-size: 11px;
}
.context-panel {
  margin-top: 8px;
  padding: 10px 12px;
  border: 1px dashed var(--el-border-color);
  border-radius: 7px;
  background: #fff;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--el-text-color-regular);
  overflow-x: auto;
}
.code-block {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: #fbfbfd;
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--el-text-color-regular);
  max-height: 260px;
  overflow: auto;
}
.ident-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 20px;
}
.ident-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.ident-item span {
  font-size: 10.5px;
  color: var(--el-text-color-secondary);
  letter-spacing: 0.02em;
}
.ident-item code {
  padding: 6px 9px;
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 11px;
  color: var(--el-text-color-regular);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* —— 抽屉内 markdown（v-html 注入需穿透） —— */
:deep(.markdown-body p) {
  margin: 4px 0;
  line-height: 1.75;
}
:deep(.markdown-body h3),
:deep(.markdown-body h4),
:deep(.markdown-body h5),
:deep(.markdown-body h6) {
  margin: 10px 0 4px;
  font-size: 14px;
}
:deep(.markdown-body ul),
:deep(.markdown-body ol) {
  margin: 4px 0;
  padding-left: 20px;
}
:deep(.markdown-body li) {
  margin: 2px 0;
}
:deep(.markdown-body code) {
  background: var(--el-fill-color);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 11.5px;
  color: var(--el-color-primary-dark-2);
}
:deep(.markdown-body img.md-image) {
  display: inline-block;
  max-width: 100%;
  max-height: 240px;
  border-radius: 6px;
  border: 1px solid var(--el-border-color-lighter);
  margin: 6px 4px 2px 0;
  vertical-align: middle;
}
:deep(.markdown-body table) {
  border-collapse: collapse;
  margin: 8px 0;
  width: 100%;
  font-size: 12px;
}
:deep(.markdown-body th),
:deep(.markdown-body td) {
  border: 1px solid var(--el-border-color-lighter);
  padding: 5px 9px;
  text-align: left;
  vertical-align: top;
  word-break: break-word;
}
:deep(.markdown-body th) {
  background: var(--el-fill-color-light);
  font-weight: 600;
}
:deep(.markdown-body td img.md-image) {
  max-height: 110px;
  margin: 2px 4px 2px 0;
}

@media (max-width: 1100px) {
  .time-picker {
    width: 280px;
  }
  .trace-row {
    grid-template-columns: 46px minmax(0, 1fr) 190px 16px;
    gap: 12px;
  }
}
@media (max-width: 960px) {
  .q-input {
    max-width: none;
  }
  .strip-note {
    display: none;
  }
  .row-stats {
    display: none;
  }
  .trace-row {
    grid-template-columns: 44px minmax(0, 1fr) 16px;
  }
  .stat-strip {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .stat-cell:nth-child(3) {
    border-right: none;
  }
  .stat-cell:nth-child(n + 4) {
    border-top: 1px solid var(--el-border-color-lighter);
  }
  .meta-grid,
  .ident-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (max-width: 720px) {
  .time-picker {
    width: 100%;
  }
  .status-select {
    width: 100%;
  }
  .detail-query {
    font-size: 17px;
  }
}
</style>
