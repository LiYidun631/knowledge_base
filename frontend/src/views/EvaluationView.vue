<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { client, errorMessage } from '../api/client'
import { canWrite } from '../api/session'
import PageHeader from '../components/ui/PageHeader.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import StatusPill from '../components/ui/StatusPill.vue'
import EmptyState from '../components/ui/EmptyState.vue'

interface Company {
  id: string
  name: string
  status: string
}

interface KnowledgeBase {
  id: string
  company_id: string | null
  name: string
  status: string
}

interface EvaluationMetrics {
  case_count: number
  completed_count: number
  errored_count: number
  hit_rate: number
  recall_at_k: number
  mrr: number
  no_result_rate: number
  avg_latency_ms: number
}

interface EvaluationRun {
  id: string
  set_id: string
  knowledge_base_id: string
  company_id: string | null
  status: 'running' | 'success' | 'failed'
  case_count: number
  completed_count: number
  metrics: EvaluationMetrics
  error_message: string | null
  started_at: string
  finished_at: string | null
  created_at: string
  knowledge_base_name: string | null
  set_name: string | null
}

interface EvaluationSet {
  id: string
  knowledge_base_id: string
  company_id: string | null
  company_name: string | null
  knowledge_base_name: string
  name: string
  description: string | null
  case_count: number
  enabled_case_count: number
  last_run: EvaluationRun | null
  created_at: string
  updated_at: string
}

interface EvaluationCase {
  id: string
  set_id: string
  query: string
  expected_document_ids: string[]
  expected_keywords: string[]
  retrieval_mode: string
  top_k: number
  is_enabled: boolean
  notes: string | null
  created_at: string
  updated_at: string
}

interface MatchItem {
  rank: number
  document_id: string | null
  filename: string | null
  page_number: number | null
  section: string | null
  matched_by: string | null
  excerpt: string
}

interface EvaluationCaseResult {
  id: string
  case_id: string | null
  query: string
  retrieval_mode: string
  top_k: number
  hit: boolean
  first_relevant_rank: number | null
  recall: number
  reciprocal_rank: number
  result_count: number
  duration_ms: number | null
  matches: MatchItem[]
  error_message: string | null
}

interface EvaluationRunDetail extends EvaluationRun {
  results: EvaluationCaseResult[]
  baseline: { id: string; finished_at: string | null; metrics: EvaluationMetrics } | null
}

const companies = ref<Company[]>([])
const kbs = ref<KnowledgeBase[]>([])
const sets = ref<EvaluationSet[]>([])
const loading = ref(false)
const selectedCompanyId = ref('')
const selectedKbId = ref('')
const keyword = ref('')

const activeCompanies = computed(() => companies.value.filter((item) => item.status === 'active'))
const activeKbs = computed(() =>
  kbs.value.filter((item) => item.status === 'active' && item.company_id === selectedCompanyId.value),
)

const modeLabel: Record<string, string> = {
  hybrid: '混合',
  bm25: '关键词',
  vector: '向量',
}
const modeOptions = Object.entries(modeLabel).map(([value, label]) => ({ value, label }))

type PillTone = 'success' | 'warning' | 'danger' | 'info' | 'primary'

const statusMeta: Record<string, { label: string; tone: PillTone }> = {
  running: { label: '运行中', tone: 'primary' },
  success: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
}

const createSet = reactive({
  open: false,
  submitting: false,
  kbId: '',
  name: '',
  description: '',
})

const drawer = reactive({
  open: false,
  loading: false,
  set: null as EvaluationSet | null,
  cases: [] as EvaluationCase[],
  runs: [] as EvaluationRun[],
})

const caseDialog = reactive({
  open: false,
  submitting: false,
  id: '',
  query: '',
  documentIds: [] as string[],
  // 手填/粘贴 UUID 的兜底入口，与下拉选择合并后去重。
  manualIds: '',
  manualMode: false,
  keywords: [] as string[],
  retrievalMode: 'hybrid',
  topK: 5,
  isEnabled: true,
  notes: '',
})

const runDialog = reactive({
  open: false,
  loading: false,
  detail: null as EvaluationRunDetail | null,
})

// 期望文档选择器：按文件名远程搜索当前知识库内的文档，候选天然限定在该库。
interface DocumentOption {
  id: string
  filename: string
}
const documentOptions = ref<DocumentOption[]>([])
const documentSearching = ref(false)

function mergeDocumentOptions(entries: DocumentOption[]) {
  const merged = new Map(documentOptions.value.map((item) => [item.id, item]))
  for (const entry of entries) merged.set(entry.id, entry)
  documentOptions.value = [...merged.values()]
}

async function searchDocuments(query: string) {
  if (!drawer.set) return
  documentSearching.value = true
  try {
    const { data } = await client.get<{ id: string; filename: string }[]>(
      `/knowledge-bases/${drawer.set.knowledge_base_id}/documents`,
      { params: { q: query.trim() || undefined, limit: 20 } },
    )
    mergeDocumentOptions(data.map((item) => ({ id: item.id, filename: item.filename })))
  } catch (error) {
    errorMessage(error, '加载文档列表失败')
  } finally {
    documentSearching.value = false
  }
}

async function resolveDocumentLabels(ids: string[]) {
  // 编辑已有用例时把 UUID 反查成文件名；文档已删除则退化为显示 UUID，不阻断编辑。
  const resolved = await Promise.all(
    ids.map(async (id) => {
      try {
        const { data } = await client.get<{ id: string; filename: string }>(
          `/documents/${id}`,
        )
        return { id: data.id, filename: data.filename }
      } catch {
        return { id, filename: id }
      }
    }),
  )
  mergeDocumentOptions(resolved)
}

let pollTimer: number | null = null

function stopPolling() {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer)
    pollTimer = null
  }
}

async function loadCompanies() {
  try {
    const { data } = await client.get<Company[]>('/companies')
    companies.value = data
    if (!selectedCompanyId.value && activeCompanies.value.length > 0) {
      selectedCompanyId.value = activeCompanies.value[0].id
    }
  } catch (error) {
    errorMessage(error, '加载公司列表失败')
  }
}

async function loadKbs() {
  kbs.value = []
  selectedKbId.value = ''
  if (!selectedCompanyId.value) return
  try {
    const { data } = await client.get<KnowledgeBase[]>('/knowledge-bases', {
      params: { company_id: selectedCompanyId.value, limit: 200 },
    })
    kbs.value = data
  } catch (error) {
    errorMessage(error, '加载知识库列表失败')
  }
}

async function loadSets() {
  loading.value = true
  try {
    const params: Record<string, unknown> = {}
    if (selectedCompanyId.value) params.company_id = selectedCompanyId.value
    if (selectedKbId.value) params.knowledge_base_id = selectedKbId.value
    if (keyword.value.trim()) params.q = keyword.value.trim()
    const { data } = await client.get<{ items: EvaluationSet[] }>('/admin/evaluation-sets', {
      params,
    })
    sets.value = data.items
  } catch (error) {
    errorMessage(error, '加载评测集失败')
  } finally {
    loading.value = false
  }
}

function search() {
  void loadSets()
}

async function onCompanyChange() {
  await loadKbs()
  search()
}

function resetFilters() {
  selectedKbId.value = ''
  keyword.value = ''
  search()
}

function openCreateSet() {
  if (!selectedCompanyId.value) {
    ElMessage.warning('请先选择公司')
    return
  }
  createSet.kbId = selectedKbId.value || activeKbs.value[0]?.id || ''
  createSet.name = ''
  createSet.description = ''
  createSet.submitting = false
  createSet.open = true
}

async function submitCreateSet() {
  const name = createSet.name.trim()
  if (!createSet.kbId) {
    ElMessage.warning('请选择知识库')
    return
  }
  if (!name) {
    ElMessage.warning('请填写评测集名称')
    return
  }
  createSet.submitting = true
  try {
    const { data } = await client.post<EvaluationSet>('/admin/evaluation-sets', {
      knowledge_base_id: createSet.kbId,
      name,
      description: createSet.description.trim() || null,
    })
    createSet.open = false
    ElMessage.success('评测集已创建')
    await loadSets()
    await openSet(data)
  } catch (error) {
    errorMessage(error, '创建评测集失败')
  } finally {
    createSet.submitting = false
  }
}

async function openSet(row: EvaluationSet) {
  drawer.open = true
  drawer.loading = true
  drawer.set = null
  drawer.cases = []
  drawer.runs = []
  try {
    const [{ data }, runsResponse] = await Promise.all([
      client.get<{ set: EvaluationSet; items: EvaluationCase[] }>(
        `/admin/evaluation-sets/${row.id}`,
      ),
      client.get<{ items: EvaluationRun[] }>('/admin/evaluation-runs', {
        params: { set_id: row.id, page_size: 20 },
      }),
    ])
    drawer.set = data.set
    drawer.cases = data.items
    drawer.runs = runsResponse.data.items
  } catch (error) {
    errorMessage(error, '加载评测集详情失败')
    drawer.open = false
  } finally {
    drawer.loading = false
  }
}

async function refreshSet() {
  const current = drawer.set
  if (!current) return
  await openSet(current)
}

async function removeSet(row: EvaluationSet) {
  try {
    await ElMessageBox.confirm(
      `删除后「${row.name}」的用例与运行记录都会一并移除，确定继续？`,
      '删除评测集',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/admin/evaluation-sets/${row.id}`)
    ElMessage.success('评测集已删除')
    if (drawer.set?.id === row.id) drawer.open = false
    await loadSets()
  } catch (error) {
    errorMessage(error, '删除评测集失败')
  }
}

function openCaseDialog(item?: EvaluationCase) {
  if (!drawer.set) return
  caseDialog.id = item?.id ?? ''
  caseDialog.query = item?.query ?? ''
  caseDialog.documentIds = [...(item?.expected_document_ids ?? [])]
  caseDialog.manualIds = ''
  caseDialog.manualMode = false
  caseDialog.keywords = [...(item?.expected_keywords ?? [])]
  caseDialog.retrievalMode = item?.retrieval_mode ?? 'hybrid'
  caseDialog.topK = item?.top_k ?? 5
  caseDialog.isEnabled = item?.is_enabled ?? true
  caseDialog.notes = item?.notes ?? ''
  caseDialog.submitting = false
  caseDialog.open = true
  // 预置可选项：已有用例先反查文件名，再拉一页默认列表供直接挑选。
  documentOptions.value = []
  if (caseDialog.documentIds.length) void resolveDocumentLabels(caseDialog.documentIds)
  void searchDocuments('')
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

async function submitCase() {
  if (!drawer.set) return
  const query = caseDialog.query.trim()
  // 下拉选择与手填粘贴合并去重，避免两种入口提交同一文档。
  const manualIds = caseDialog.manualIds
    .split(/[\s,，;；]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  const documentIds = [...new Set([...caseDialog.documentIds, ...manualIds])]
  const keywords = caseDialog.keywords.map((item) => item.trim()).filter(Boolean)
  if (!query) {
    ElMessage.warning('请填写问题')
    return
  }
  if (documentIds.length === 0 && keywords.length === 0) {
    ElMessage.warning('期望文档与关键词至少填写一项')
    return
  }
  if (documentIds.some((item) => !UUID_RE.test(item))) {
    ElMessage.warning('期望文档 ID 需为 UUID，请检查格式')
    return
  }
  const payload = {
    query,
    expected_document_ids: documentIds,
    expected_keywords: keywords,
    retrieval_mode: caseDialog.retrievalMode,
    top_k: caseDialog.topK,
    is_enabled: caseDialog.isEnabled,
    notes: caseDialog.notes.trim() || null,
  }
  caseDialog.submitting = true
  try {
    if (caseDialog.id) {
      await client.patch(`/admin/evaluation-cases/${caseDialog.id}`, payload)
    } else {
      await client.post(`/admin/evaluation-sets/${drawer.set.id}/cases`, payload)
    }
    caseDialog.open = false
    ElMessage.success(caseDialog.id ? '用例已更新' : '用例已添加')
    await refreshSet()
    await loadSets()
  } catch (error) {
    errorMessage(error, '保存用例失败')
  } finally {
    caseDialog.submitting = false
  }
}

async function toggleCase(item: EvaluationCase) {
  try {
    const { data } = await client.patch<EvaluationCase>(`/admin/evaluation-cases/${item.id}`, {
      is_enabled: !item.is_enabled,
    })
    item.is_enabled = data.is_enabled
    if (drawer.set) {
      drawer.set.enabled_case_count = drawer.cases.filter((entry) => entry.is_enabled).length
    }
  } catch (error) {
    errorMessage(error, '更新用例状态失败')
  }
}

async function removeCase(item: EvaluationCase) {
  try {
    await ElMessageBox.confirm(`删除用例「${item.query}」？`, '删除用例', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await client.delete(`/admin/evaluation-cases/${item.id}`)
    ElMessage.success('用例已删除')
    await refreshSet()
    await loadSets()
  } catch (error) {
    errorMessage(error, '删除用例失败')
  }
}

function schedulePoll(runId: string) {
  stopPolling()
  pollTimer = window.setTimeout(async () => {
    pollTimer = null
    try {
      const { data } = await client.get<EvaluationRunDetail>(`/admin/evaluation-runs/${runId}`)
      if (data.status === 'running') {
        if (drawer.set?.id === data.set_id) {
          drawer.set.last_run = data
          const index = drawer.runs.findIndex((item) => item.id === data.id)
          if (index >= 0) drawer.runs[index] = data
        }
        schedulePoll(runId)
        return
      }
      if (data.status === 'success') ElMessage.success('评测完成')
      else ElMessage.error(data.error_message || '评测失败')
      await loadSets()
      if (drawer.open) await refreshSet()
      if (runDialog.open) await showRun(runId)
    } catch {
      // 轮询失败时保留列表状态，下次手动刷新即可。
    }
  }, 1200)
}

async function startRun(row: EvaluationSet) {
  if (row.enabled_case_count === 0) {
    ElMessage.warning('该评测集没有启用中的用例')
    return
  }
  try {
    const { data } = await client.post<EvaluationRun>(
      `/admin/evaluation-sets/${row.id}/run`,
    )
    ElMessage.success('评测已启动，完成后会显示指标')
    if (drawer.set?.id === row.id) {
      drawer.set.last_run = data
    }
    await loadSets()
    schedulePoll(data.id)
  } catch (error) {
    errorMessage(error, '启动评测失败')
  }
}

async function showRun(runId: string) {
  runDialog.open = true
  runDialog.loading = true
  runDialog.detail = null
  try {
    const { data } = await client.get<EvaluationRunDetail>(`/admin/evaluation-runs/${runId}`)
    runDialog.detail = data
  } catch (error) {
    errorMessage(error, '加载运行详情失败')
    runDialog.open = false
  } finally {
    runDialog.loading = false
  }
}

async function removeRun(run: EvaluationRun) {
  try {
    await ElMessageBox.confirm('删除这条运行记录？', '删除运行记录', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await client.delete(`/admin/evaluation-runs/${run.id}`)
    ElMessage.success('运行记录已删除')
    await refreshSet()
    await loadSets()
  } catch (error) {
    errorMessage(error, '删除运行记录失败')
  }
}

function formatDateTime(value: string | null): string {
  return value ? value.slice(0, 19).replace('T', ' ') : '—'
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`
}

function score(value: number | null | undefined, digits = 3): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(2)} s`
}

// 与同集上一次成功运行对比，返回带符号的差值文案。
function delta(current: number, baseline: number | null | undefined, digits = 1): string {
  if (baseline === null || baseline === undefined) return ''
  const value = (current - baseline) * 100
  if (Math.abs(value) < 0.05) return '持平'
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}%`
}

function deltaTone(current: number, baseline: number | null | undefined): string {
  if (baseline === null || baseline === undefined) return ''
  const value = (current - baseline) * 100
  if (Math.abs(value) < 0.05) return 'is-flat'
  return value > 0 ? 'is-up' : 'is-down'
}

function lastRunTone(row: EvaluationSet): PillTone {
  return statusMeta[row.last_run?.status ?? '']?.tone ?? 'info'
}

function lastRunLabel(row: EvaluationSet): string {
  if (!row.last_run) return '未运行'
  return statusMeta[row.last_run.status]?.label ?? row.last_run.status
}

onMounted(async () => {
  await loadCompanies()
  await loadKbs()
  await loadSets()
})

onUnmounted(stopPolling)
</script>

<template>
  <div class="eval-page">
    <PageHeader
      title="检索评测"
      eyebrow="质量回归"
      subtitle="用固定问题回归检索质量，不生成回答、不消耗聊天 Token。"
    >
      <template #meta>
        <span class="header-count">{{ sets.length }} 个评测集</span>
      </template>
      <template #actions>
        <el-button v-if="canWrite" type="primary" size="small" @click="openCreateSet">
          <el-icon><Plus /></el-icon>
          新建评测集
        </el-button>
      </template>
    </PageHeader>

    <FilterBar>
      <el-select
        v-model="selectedCompanyId"
        placeholder="全部公司"
        class="company-select"
        @change="onCompanyChange"
      >
        <el-option
          v-for="company in activeCompanies"
          :key="company.id"
          :label="company.name"
          :value="company.id"
        />
      </el-select>
      <el-select
        v-model="selectedKbId"
        clearable
        placeholder="全部知识库"
        class="kb-select"
        :disabled="!selectedCompanyId"
        @change="search"
      >
        <el-option v-for="kb in activeKbs" :key="kb.id" :label="kb.name" :value="kb.id" />
      </el-select>
      <el-input
        v-model="keyword"
        clearable
        placeholder="搜索评测集名称"
        class="q-input"
        @keyup.enter="search"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <template #actions>
        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </template>
    </FilterBar>

    <div v-loading="loading" class="set-grid">
      <article
        v-for="row in sets"
        :key="row.id"
        class="set-card"
        :class="{ 'is-idle': !row.last_run }"
        @click="openSet(row)"
      >
        <header class="set-head">
          <div class="set-title-wrap">
            <h3 class="set-name">{{ row.name }}</h3>
            <span class="set-scope">{{ row.company_name ?? '通用' }} · {{ row.knowledge_base_name }}</span>
          </div>
          <StatusPill
            :label="lastRunLabel(row)"
            :tone="lastRunTone(row)"
            :pulse="row.last_run?.status === 'running'"
          />
        </header>

        <p class="set-desc">{{ row.description || '暂无说明' }}</p>

        <div v-if="row.last_run?.status === 'success'" class="set-metrics">
          <div class="metric">
            <strong>{{ percent(row.last_run.metrics.hit_rate) }}</strong>
            <span>命中率</span>
          </div>
          <div class="metric">
            <strong>{{ score(row.last_run.metrics.mrr) }}</strong>
            <span>MRR</span>
          </div>
          <div class="metric">
            <strong>{{ formatMs(row.last_run.metrics.avg_latency_ms) }}</strong>
            <span>平均耗时</span>
          </div>
        </div>
        <div v-else class="set-metrics is-placeholder">
          <span>{{ row.last_run?.status === 'failed' ? row.last_run.error_message || '上次运行失败' : '运行一次后在这里查看命中率与 MRR' }}</span>
        </div>

        <footer class="set-foot">
          <span class="case-count">
            <strong>{{ row.enabled_case_count }}</strong> / {{ row.case_count }} 条启用
          </span>
          <div class="card-actions">
            <el-button
              v-if="canWrite"
              size="small"
              :disabled="row.enabled_case_count === 0"
              @click.stop="startRun(row)"
            >
              运行
            </el-button>
            <el-button size="small" type="primary" plain @click.stop="openSet(row)">用例</el-button>
          </div>
        </footer>
      </article>

      <EmptyState
        v-if="!loading && sets.length === 0"
        class="grid-empty"
        title="还没有评测集"
        text="先建一个知识库回归集，录入问题与期望命中的文档或关键词，再一键跑基线。"
      >
        <el-button v-if="canWrite" type="primary" @click="openCreateSet">新建评测集</el-button>
      </EmptyState>
    </div>

    <el-drawer v-model="drawer.open" size="820px" :with-header="false">
      <div v-loading="drawer.loading" class="drawer-shell">
        <template v-if="drawer.set">
          <header class="drawer-head">
            <div class="drawer-eyebrow">
              <span>{{ drawer.set.company_name ?? '通用' }}</span>
              <i class="crumb-sep" />
              <span>{{ drawer.set.knowledge_base_name }}</span>
            </div>
            <h2 class="drawer-title">{{ drawer.set.name }}</h2>
            <p class="drawer-desc">{{ drawer.set.description || '暂无说明' }}</p>
            <div class="drawer-actions">
              <template v-if="canWrite">
                <el-button
                  type="primary"
                  :disabled="drawer.set.enabled_case_count === 0"
                  @click="startRun(drawer.set)"
                >
                  <el-icon><VideoPlay /></el-icon>
                  运行评测
                </el-button>
                <el-button @click="openCaseDialog()">新增用例</el-button>
                <el-button type="danger" plain @click="removeSet(drawer.set)">删除评测集</el-button>
              </template>
              <button class="drawer-close" type="button" title="关闭" @click="drawer.open = false">
                <el-icon><Close /></el-icon>
              </button>
            </div>
          </header>

          <section class="drawer-section">
            <div class="section-head">
              <h3>用例</h3>
              <span class="section-aside">{{ drawer.set.enabled_case_count }} / {{ drawer.cases.length }} 条启用</span>
            </div>
            <el-table :data="drawer.cases" size="small" empty-text="还没有用例">
              <el-table-column prop="query" label="问题" min-width="200" show-overflow-tooltip />
              <el-table-column label="期望" min-width="160">
                <template #default="{ row }">
                  <span class="expect-cell">
                    <span v-if="row.expected_document_ids.length">文档 {{ row.expected_document_ids.length }}</span>
                    <span v-if="row.expected_keywords.length" class="expect-keywords">
                      关键词 {{ row.expected_keywords.join(' / ') }}
                    </span>
                    <span v-if="!row.expected_document_ids.length && !row.expected_keywords.length">—</span>
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="模式" width="70">
                <template #default="{ row }">{{ modeLabel[row.retrieval_mode] ?? row.retrieval_mode }}</template>
              </el-table-column>
              <el-table-column prop="top_k" label="Top K" width="64" align="center" />
              <el-table-column label="启用" width="64" align="center">
                <template #default="{ row }">
                  <el-switch
                    v-if="canWrite"
                    :model-value="row.is_enabled"
                    size="small"
                    @change="toggleCase(row)"
                  />
                  <StatusPill
                    v-else
                    :label="row.is_enabled ? '启用' : '停用'"
                    :tone="row.is_enabled ? 'success' : 'info'"
                  />
                </template>
              </el-table-column>
              <el-table-column v-if="canWrite" label="操作" width="110" align="right">
                <template #default="{ row }">
                  <el-button link type="primary" size="small" @click="openCaseDialog(row)">编辑</el-button>
                  <el-button link type="danger" size="small" @click="removeCase(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </section>

          <section class="drawer-section">
            <div class="section-head">
              <h3>运行记录</h3>
              <span class="section-aside">{{ drawer.runs.length }} 次</span>
            </div>
            <el-table :data="drawer.runs" size="small" empty-text="还没有运行记录">
              <el-table-column label="开始时间" width="160">
                <template #default="{ row }">{{ formatDateTime(row.started_at) }}</template>
              </el-table-column>
              <el-table-column label="状态" width="100">
                <template #default="{ row }">
                  <StatusPill
                    :label="statusMeta[row.status]?.label ?? row.status"
                    :tone="statusMeta[row.status]?.tone ?? 'info'"
                    :pulse="row.status === 'running'"
                  />
                </template>
              </el-table-column>
              <el-table-column label="命中率" width="84" align="right">
                <template #default="{ row }">{{ row.status === 'success' ? percent(row.metrics.hit_rate) : '—' }}</template>
              </el-table-column>
              <el-table-column label="MRR" width="70" align="right">
                <template #default="{ row }">{{ row.status === 'success' ? score(row.metrics.mrr) : '—' }}</template>
              </el-table-column>
              <el-table-column label="用例" width="88" align="right">
                <template #default="{ row }">{{ row.completed_count }} / {{ row.case_count }}</template>
              </el-table-column>
              <el-table-column label="耗时" width="90" align="right">
                <template #default="{ row }">{{ formatMs(row.metrics.avg_latency_ms) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="104" align="right">
                <template #default="{ row }">
                  <el-button link type="primary" size="small" @click="showRun(row.id)">详情</el-button>
                  <el-button
                    v-if="canWrite"
                    link
                    type="danger"
                    size="small"
                    :disabled="row.status === 'running'"
                    @click="removeRun(row)"
                  >
                    删除
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </section>
        </template>
      </div>
    </el-drawer>

    <el-dialog v-model="createSet.open" title="新建评测集" width="520px">
      <el-form label-position="top">
        <el-form-item label="知识库">
          <el-select v-model="createSet.kbId" placeholder="选择知识库" class="full-width">
            <el-option v-for="kb in activeKbs" :key="kb.id" :label="kb.name" :value="kb.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="createSet.name" maxlength="160" placeholder="例如：退款政策回归集" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input
            v-model="createSet.description"
            type="textarea"
            :rows="3"
            maxlength="1000"
            placeholder="记录这批评测覆盖的业务范围（可选）"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createSet.open = false">取消</el-button>
        <el-button type="primary" :loading="createSet.submitting" @click="submitCreateSet">
          创建
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="caseDialog.open"
      :title="caseDialog.id ? '编辑用例' : '新增用例'"
      width="620px"
    >
      <el-form label-position="top">
        <el-form-item label="问题">
          <el-input
            v-model="caseDialog.query"
            type="textarea"
            :rows="2"
            maxlength="4000"
            placeholder="用户真实会问的问题"
          />
        </el-form-item>
        <el-form-item label="期望文档">
          <el-select
            v-model="caseDialog.documentIds"
            multiple
            filterable
            remote
            reserve-keyword
            :remote-method="searchDocuments"
            :loading="documentSearching"
            placeholder="输入文件名搜索本库文档，可多选"
            class="full-width"
          >
            <el-option
              v-for="doc in documentOptions"
              :key="doc.id"
              :label="doc.filename"
              :value="doc.id"
            >
              <span
                style="
                  display: inline-block;
                  max-width: 76%;
                  overflow: hidden;
                  text-overflow: ellipsis;
                  white-space: nowrap;
                  vertical-align: bottom;
                "
              >{{ doc.filename }}</span>
              <span
                style="
                  float: right;
                  margin-left: 12px;
                  font-size: 12px;
                  color: var(--el-text-color-placeholder);
                "
              >{{ doc.id.slice(0, 8) }}</span>
            </el-option>
          </el-select>
          <div class="manual-toggle">
            <el-button
              link
              type="primary"
              size="small"
              @click="caseDialog.manualMode = !caseDialog.manualMode"
            >
              {{ caseDialog.manualMode ? '收起手填 UUID' : '手填 / 粘贴 UUID' }}
            </el-button>
          </div>
          <el-input
            v-if="caseDialog.manualMode"
            v-model="caseDialog.manualIds"
            type="textarea"
            :rows="3"
            placeholder="每行一个文档 UUID；与上方已选合并去重"
          />
        </el-form-item>
        <el-form-item label="期望关键词">
          <el-select
            v-model="caseDialog.keywords"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="输入后回车，正文命中任一关键词即算相关"
            class="full-width"
          />
        </el-form-item>
        <p class="field-hint">文档与关键词是「或」关系，至少填写一项；填写文档时召回率按文档覆盖率计算。</p>
        <div class="field-row">
          <el-form-item label="检索模式" class="field-half">
            <el-select v-model="caseDialog.retrievalMode">
              <el-option
                v-for="item in modeOptions"
                :key="item.value"
                :label="item.label"
                :value="item.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="Top K" class="field-half">
            <el-input-number v-model="caseDialog.topK" :min="1" :max="20" />
          </el-form-item>
          <el-form-item label="启用" class="field-half">
            <el-switch v-model="caseDialog.isEnabled" />
          </el-form-item>
        </div>
        <el-form-item label="备注">
          <el-input v-model="caseDialog.notes" maxlength="1000" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="caseDialog.open = false">取消</el-button>
        <el-button type="primary" :loading="caseDialog.submitting" @click="submitCase">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="runDialog.open" title="评测结果" width="920px">
      <div v-loading="runDialog.loading" class="run-detail">
        <template v-if="runDialog.detail">
          <div class="run-summary">
            <div class="summary-item">
              <span>命中率</span>
              <strong>{{ percent(runDialog.detail.metrics.hit_rate) }}</strong>
              <em
                v-if="runDialog.detail.baseline"
                :class="deltaTone(runDialog.detail.metrics.hit_rate, runDialog.detail.baseline.metrics.hit_rate)"
              >
                {{ delta(runDialog.detail.metrics.hit_rate, runDialog.detail.baseline.metrics.hit_rate) }}
              </em>
            </div>
            <div class="summary-item">
              <span>平均召回</span>
              <strong>{{ percent(runDialog.detail.metrics.recall_at_k) }}</strong>
              <em
                v-if="runDialog.detail.baseline"
                :class="deltaTone(runDialog.detail.metrics.recall_at_k, runDialog.detail.baseline.metrics.recall_at_k)"
              >
                {{ delta(runDialog.detail.metrics.recall_at_k, runDialog.detail.baseline.metrics.recall_at_k) }}
              </em>
            </div>
            <div class="summary-item">
              <span>MRR</span>
              <strong>{{ score(runDialog.detail.metrics.mrr) }}</strong>
              <em
                v-if="runDialog.detail.baseline"
                :class="deltaTone(runDialog.detail.metrics.mrr, runDialog.detail.baseline.metrics.mrr)"
              >
                {{ delta(runDialog.detail.metrics.mrr, runDialog.detail.baseline.metrics.mrr) }}
              </em>
            </div>
            <div class="summary-item">
              <span>无结果率</span>
              <strong>{{ percent(runDialog.detail.metrics.no_result_rate) }}</strong>
            </div>
            <div class="summary-item">
              <span>平均耗时</span>
              <strong>{{ formatMs(runDialog.detail.metrics.avg_latency_ms) }}</strong>
            </div>
            <div class="summary-item">
              <span>用例</span>
              <strong>{{ runDialog.detail.metrics.completed_count }} / {{ runDialog.detail.metrics.case_count }}</strong>
              <em v-if="runDialog.detail.metrics.errored_count">失败 {{ runDialog.detail.metrics.errored_count }}</em>
            </div>
          </div>

          <p v-if="runDialog.detail.baseline" class="baseline-note">
            对比基线：{{ formatDateTime(runDialog.detail.baseline.finished_at) }} 的成功运行
          </p>

          <el-table :data="runDialog.detail.results" size="small" max-height="420">
            <el-table-column type="expand">
              <template #default="{ row }">
                <div class="match-list">
                  <div v-for="match in row.matches" :key="match.rank" class="match-row" :class="{ relevant: match.matched_by }">
                    <span class="match-rank">#{{ match.rank }}</span>
                    <span class="match-file">{{ match.filename ?? '—' }}</span>
                    <span v-if="match.page_number" class="match-meta">第 {{ match.page_number }} 页</span>
                    <span v-if="match.section" class="match-meta">{{ match.section }}</span>
                    <StatusPill
                      v-if="match.matched_by"
                      :label="match.matched_by === 'document' ? '文档命中' : '关键词命中'"
                      tone="success"
                    />
                    <p class="match-excerpt">{{ match.excerpt }}</p>
                  </div>
                  <p v-if="!row.matches.length" class="match-empty">本次没有召回片段</p>
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="query" label="问题" min-width="200" show-overflow-tooltip />
            <el-table-column label="命中" width="76" align="center">
              <template #default="{ row }">
                <StatusPill
                  :label="row.hit ? '是' : '否'"
                  :tone="row.hit ? 'success' : 'danger'"
                  :dot="false"
                />
              </template>
            </el-table-column>
            <el-table-column label="首个相关" width="86" align="center">
              <template #default="{ row }">{{ row.first_relevant_rank ?? '—' }}</template>
            </el-table-column>
            <el-table-column label="召回率" width="80" align="right">
              <template #default="{ row }">{{ percent(row.recall) }}</template>
            </el-table-column>
            <el-table-column prop="result_count" label="结果数" width="76" align="right" />
            <el-table-column label="耗时" width="86" align="right">
              <template #default="{ row }">{{ formatMs(row.duration_ms) }}</template>
            </el-table-column>
            <el-table-column label="错误" min-width="120" show-overflow-tooltip>
              <template #default="{ row }">{{ row.error_message ?? '—' }}</template>
            </el-table-column>
          </el-table>
        </template>
      </div>
      <template #footer>
        <el-button @click="runDialog.open = false">关闭</el-button>
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
.company-select {
  width: 160px;
}
.kb-select {
  width: 180px;
}
.q-input {
  width: 200px;
}

.set-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
  gap: 14px;
  min-height: 220px;
}
.set-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 236px;
  padding: 16px 18px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--el-border-radius-base);
  background:
    linear-gradient(180deg, rgba(79, 70, 229, 0.035), transparent 46%),
    #fff;
  cursor: pointer;
  transition: border-color 0.18s var(--kb-ease), box-shadow 0.18s var(--kb-ease),
    transform 0.18s var(--kb-ease);
  animation: card-in 0.32s var(--kb-ease) backwards;
}
.set-card:hover {
  border-color: var(--el-color-primary-light-7);
  box-shadow: 0 14px 28px -22px rgba(49, 46, 129, 0.55);
  transform: translateY(-2px);
}
.set-card:active {
  transform: translateY(0) scale(0.995);
}
@keyframes card-in {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
.set-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.set-title-wrap {
  min-width: 0;
}
.set-name {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -0.1px;
  color: var(--el-text-color-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.set-scope {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.set-desc {
  margin: 0;
  min-height: 34px;
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.set-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  padding: 10px 12px;
  border-radius: var(--el-border-radius-small);
  background: var(--el-fill-color-lighter);
}
.metric {
  min-width: 0;
}
.metric strong {
  display: block;
  font-size: 15px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-primary);
}
.metric span {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.set-metrics.is-placeholder {
  display: flex;
  align-items: center;
  min-height: 54px;
  font-size: 12px;
  color: var(--el-text-color-placeholder);
  line-height: 1.5;
}
.set-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: auto;
  padding-top: 10px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.case-count {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}
.case-count strong {
  color: var(--el-text-color-primary);
}
.card-actions {
  display: flex;
  gap: 6px;
}
.grid-empty {
  grid-column: 1 / -1;
  border: 1px dashed var(--el-border-color);
  border-radius: var(--kb-panel-radius);
  background: #fff;
}

.drawer-shell {
  min-height: 320px;
}
.drawer-head {
  position: relative;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.drawer-eyebrow {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.crumb-sep {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--el-text-color-placeholder);
}
.drawer-title {
  margin: 8px 0 4px;
  font-size: 20px;
  font-weight: 650;
  letter-spacing: -0.2px;
}
.drawer-desc {
  margin: 0 0 14px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.drawer-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.drawer-close {
  margin-left: auto;
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 50%;
  background: #fff;
  color: var(--el-text-color-secondary);
  cursor: pointer;
  transition: color 0.15s var(--kb-ease), border-color 0.15s var(--kb-ease);
}
.drawer-close:hover {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary-light-6);
}
.drawer-section {
  margin-top: 22px;
}
.section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 10px;
}
.section-head h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
}
.section-head h3::before {
  content: '';
  display: inline-block;
  width: 4px;
  height: 14px;
  border-radius: 999px;
  background: var(--el-color-primary);
  margin-right: 8px;
  vertical-align: -2px;
}
.section-aside {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.expect-cell {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 12px;
  color: var(--el-text-color-regular);
}
.expect-keywords {
  color: var(--el-text-color-secondary);
}
.full-width {
  width: 100%;
}
.manual-toggle {
  margin: 2px 0 4px;
  line-height: 1.2;
}
.field-hint {
  margin: -4px 0 12px;
  font-size: 12px;
  color: var(--el-text-color-placeholder);
  line-height: 1.5;
}
.field-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
.run-detail {
  min-height: 120px;
}
.run-summary {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 10px;
  padding: 14px 16px;
  border-radius: var(--el-border-radius-base);
  background: var(--el-fill-color-lighter);
}
.summary-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.summary-item span {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.summary-item strong {
  font-size: 16px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.summary-item em {
  font-style: normal;
  font-size: 11px;
  color: var(--el-text-color-placeholder);
}
.summary-item em.is-up {
  color: var(--el-color-success);
}
.summary-item em.is-down {
  color: var(--el-color-danger);
}
.summary-item em.is-flat {
  color: var(--el-text-color-secondary);
}
.baseline-note {
  margin: 10px 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.match-list {
  padding: 4px 12px 10px;
}
.match-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--el-border-radius-small);
  font-size: 12px;
}
.match-row.relevant {
  background: var(--el-color-success-light-9);
}
.match-rank {
  font-weight: 650;
  color: var(--el-color-primary);
}
.match-file {
  font-weight: 550;
}
.match-meta {
  color: var(--el-text-color-secondary);
}
.match-excerpt {
  flex-basis: 100%;
  margin: 2px 0 0;
  color: var(--el-text-color-regular);
  line-height: 1.6;
}
.match-empty {
  margin: 0;
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}
@media (max-width: 1200px) {
  .run-summary {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
</style>
