<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import axios from 'axios'
import { ElMessage } from 'element-plus'
import { client, errorMessage } from '../api/client'
import { canWrite } from '../api/session'
import { renderMarkdown } from '../utils/markdown'
import { resolveInlineImages } from '../utils/assets'
import { formatBeijingDateTime } from '../utils/datetime'
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

interface QARecordItem {
  id: string
  request_id: string
  company_id: string | null
  company_name: string | null
  knowledge_base_id: string
  knowledge_base_name: string
  query: string
  answer: string | null
  retrieval_mode: string
  top_k: number
  include_context: boolean
  status: 'running' | 'success' | 'no_results' | 'failed'
  review_status: 'none' | 'pending' | 'resolved'
  feedback: 'up' | 'down' | null
  feedback_reason: string | null
  feedback_at: string | null
  faq_matched: boolean
  result_count: number
  chat_provider: string | null
  chat_model_name: string | null
  retrieval_duration_ms: number | null
  generation_duration_ms: number | null
  total_duration_ms: number | null
  prompt_tokens: number | null
  completion_tokens: number | null
  total_tokens: number | null
  error_code: string | null
  error_message: string | null
  created_at: string
}

interface QASource {
  rank: number
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
}

interface QAEvent {
  stage: string
  status: 'started' | 'success' | 'failed'
  duration_ms: number | null
  error_code: string | null
  message: string | null
  details: Record<string, unknown>
  created_at: string
}

interface QARecordDetail extends QARecordItem {
  sources: QASource[]
  events: QAEvent[]
}

const companies = ref<Company[]>([])
const kbs = ref<KnowledgeBase[]>([])
const rows = ref<QARecordItem[]>([])
const total = ref(0)
const loading = ref(false)
const route = useRoute()

const filters = reactive({
  companyId: '',
  kbId: '',
  status: '',
  reviewStatus: '',
  feedback: '',
  faqMatched: '',
  q: '',
  timeRange: [] as string[],
})

const pager = reactive({
  page: 1,
  pageSize: 20,
})

type Tone = 'success' | 'warning' | 'danger' | 'info' | 'primary'

const statusMeta: Record<string, { label: string; tone: Tone }> = {
  success: { label: '成功', tone: 'success' },
  no_results: { label: '未命中', tone: 'warning' },
  failed: { label: '失败', tone: 'danger' },
  running: { label: '处理中', tone: 'info' },
}

const reviewMeta: Record<string, { label: string; tone: Tone }> = {
  none: { label: '未复核', tone: 'info' },
  pending: { label: '待补资料', tone: 'warning' },
  resolved: { label: '已补充', tone: 'success' },
}

const feedbackMeta: Record<string, { label: string; tone: Tone }> = {
  up: { label: '有用', tone: 'success' },
  down: { label: '没用', tone: 'danger' },
}

const modeLabel: Record<string, string> = {
  hybrid: '混合',
  bm25: '关键词',
  vector: '向量',
}

const stageLabel: Record<string, string> = {
  request: '请求',
  retrieval: '检索',
  generation: '生成',
}

const drawer = reactive({
  open: false,
  loading: false,
  detail: null as QARecordDetail | null,
})

// 差评沉淀：把问答记录里的问题与回答整理成 FAQ，随后自动标记复核完成。
const faqDialog = reactive({
  open: false,
  submitting: false,
  question: '',
  answer: '',
})

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

async function loadRecords() {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: pager.page,
      page_size: pager.pageSize,
      ...filterParams(),
    }
    const { data } = await client.get('/admin/qa-records', { params })
    rows.value = data.items
    total.value = data.total
  } catch (error) {
    errorMessage(error, '加载问答记录失败')
  } finally {
    loading.value = false
  }
}

// 列表与导出共用筛选参数，保证导出的就是当前筛选结果。
function filterParams(): Record<string, unknown> {
  const params: Record<string, unknown> = {}
  if (filters.companyId) params.company_id = filters.companyId
  if (filters.kbId) params.knowledge_base_id = filters.kbId
  if (filters.status) params.status = filters.status
  if (filters.reviewStatus) params.review_status = filters.reviewStatus
  if (filters.feedback) params.feedback = filters.feedback
  if (filters.faqMatched) params.faq_matched = filters.faqMatched
  if (filters.q.trim()) params.q = filters.q.trim()
  if (filters.timeRange.length === 2) {
    params.start_time = filters.timeRange[0]
    params.end_time = filters.timeRange[1]
  }
  return params
}

const exporting = ref(false)

async function exportCsv() {
  if (!total.value) {
    ElMessage.warning('当前筛选没有可导出的记录')
    return
  }
  exporting.value = true
  try {
    const response = await client.get('/admin/qa-records/export', {
      params: filterParams(),
      responseType: 'blob',
    })
    const blob = response.data as Blob
    const disposition = String(response.headers['content-disposition'] ?? '')
    const matched = /filename="([^"]+)"/.exec(disposition)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = matched?.[1] ?? 'qa-records.csv'
    link.click()
    URL.revokeObjectURL(url)
    ElMessage.success(`已导出 ${total.value} 条记录`)
  } catch (error) {
    // responseType 为 blob 时错误体也是 Blob，需手动解出 detail（如超出导出上限）。
    if (axios.isAxiosError(error) && error.response?.data instanceof Blob) {
      let detail = ''
      try {
        detail = (JSON.parse(await error.response.data.text()) as { detail?: string }).detail ?? ''
      } catch {
        // 非 JSON 错误体，走通用提示。
      }
      if (detail) {
        ElMessage.error(detail)
        return
      }
    }
    errorMessage(error, '导出失败')
  } finally {
    exporting.value = false
  }
}

function search() {
  pager.page = 1
  loadRecords()
}

// 低频筛选收进「更多筛选」浮层，主筛选条保持单行。
const advancedOpen = ref(false)

const advancedCount = computed(
  () =>
    [filters.reviewStatus, filters.feedback, filters.faqMatched].filter(Boolean).length +
    (filters.timeRange.length === 2 ? 1 : 0),
)

function resetAdvanced() {
  filters.reviewStatus = ''
  filters.feedback = ''
  filters.faqMatched = ''
  filters.timeRange = []
  search()
}

function applyAdvanced() {
  advancedOpen.value = false
  search()
}

function resetFilters() {
  filters.companyId = ''
  filters.kbId = ''
  filters.status = ''
  filters.reviewStatus = ''
  filters.feedback = ''
  filters.faqMatched = ''
  filters.q = ''
  filters.timeRange = []
  kbs.value = []
  search()
}

async function openDetail(row: QARecordItem) {
  drawer.open = true
  drawer.loading = true
  drawer.detail = null
  try {
    const { data } = await client.get<QARecordDetail>(`/admin/qa-records/${row.id}`)
    // 答案与来源快照里的图片需带鉴权拉取，先解析再渲染。
    const [answer, sources] = await Promise.all([
      data.answer ? resolveInlineImages(data.answer) : Promise.resolve(data.answer),
      Promise.all(
        data.sources.map(async (source) => ({
          ...source,
          content_snapshot: await resolveInlineImages(source.content_snapshot),
        })),
      ),
    ])
    drawer.detail = {
      ...data,
      answer,
      sources,
      company_name: row.company_name,
      knowledge_base_name: row.knowledge_base_name,
    }
  } catch (error) {
    errorMessage(error, '加载问答详情失败')
    drawer.open = false
  } finally {
    drawer.loading = false
  }
}

async function toggleReview() {
  const detail = drawer.detail
  if (!detail) return
  const target = detail.review_status === 'resolved' ? 'none' : 'resolved'
  try {
    await client.patch(`/admin/qa-records/${detail.id}/review`, { review_status: target })
    detail.review_status = target
    const row = rows.value.find((item) => item.id === detail.id)
    if (row) row.review_status = target
    ElMessage.success(target === 'resolved' ? '已标记为已补充' : '已重新打开复核')
    // 待补资料视图下解决后应从列表消失。
    if (filters.reviewStatus === 'pending' && target === 'resolved') await loadRecords()
  } catch (error) {
    errorMessage(error, '更新复核状态失败')
  }
}

watch(
  () => filters.companyId,
  async () => {
    filters.kbId = ''
    await loadKbs()
    search()
  },
)

function openFaqDialog() {
  const detail = drawer.detail
  if (!detail?.answer) return
  faqDialog.question = detail.query
  faqDialog.answer = detail.answer
  faqDialog.open = true
}

async function submitFaq() {
  const detail = drawer.detail
  if (!detail) return
  const question = faqDialog.question.trim()
  const answer = faqDialog.answer.trim()
  if (!question || !answer) {
    ElMessage.warning('问题和答案都不能为空')
    return
  }
  faqDialog.submitting = true
  try {
    await client.post(`/admin/knowledge-bases/${detail.knowledge_base_id}/faqs`, {
      question,
      answer,
    })
    if (detail.review_status !== 'resolved') {
      await client.patch(`/admin/qa-records/${detail.id}/review`, { review_status: 'resolved' })
      detail.review_status = 'resolved'
      const row = rows.value.find((item) => item.id === detail.id)
      if (row) row.review_status = 'resolved'
    }
    faqDialog.open = false
    ElMessage.success('已沉淀为 FAQ，并标记复核完成')
  } catch (error) {
    errorMessage(error, '沉淀 FAQ 失败')
  } finally {
    faqDialog.submitting = false
  }
}

function formatDateTime(value: string): string {
  // 库存 UTC，界面按北京时间展示。
  return formatBeijingDateTime(value)
}

function formatMs(value: number | null): string {
  if (value === null || value === undefined) return '—'
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(2)} s`
}

function formatScore(value: number | null): string {
  return value === null || value === undefined ? '—' : value.toFixed(4)
}

// rerank 是 0~1 的置信度，用三档色阶条让低分来源一眼可见。
function scoreTier(score: number | null): 'high' | 'mid' | 'low' | 'none' {
  if (score === null || score === undefined) return 'none'
  if (score >= 0.7) return 'high'
  if (score >= 0.4) return 'mid'
  return 'low'
}

onMounted(async () => {
  await loadCompanies()
  // 从统计概览的待补资料卡片跳转时自动带上复核过滤。
  if (route.query.review === 'pending') filters.reviewStatus = 'pending'
  // 全局搜索跳转时带上关键词，直接列出相关问答。
  if (typeof route.query.q === 'string') filters.q = route.query.q
  await loadRecords()
})
</script>

<template>
  <div class="qa-page">
    <PageHeader
      title="问答记录"
      eyebrow="运营台账"
      subtitle="每次提问的状态、耗时与来源快照逐条留档，差评可直接沉淀为 FAQ。"
    >
      <template #meta>
        <span class="header-count">共 {{ total }} 条</span>
      </template>
      <template #actions>
        <el-button size="small" :loading="exporting" @click="exportCsv">
          <el-icon><Download /></el-icon>导出 CSV
        </el-button>
      </template>
    </PageHeader>

    <DataPanel flush>
      <div class="panel-toolbar">
        <FilterBar>
          <el-select
            v-model="filters.companyId"
            clearable
            placeholder="全部公司"
            class="company-select"
          >
            <el-option v-for="company in companies" :key="company.id" :label="company.name" :value="company.id" />
          </el-select>
          <el-select
            v-model="filters.kbId"
            clearable
            placeholder="全部知识库"
            class="kb-select"
            :disabled="!filters.companyId"
          >
            <el-option v-for="kb in kbs" :key="kb.id" :label="kb.name" :value="kb.id" />
          </el-select>
          <el-select v-model="filters.status" clearable placeholder="全部状态" class="status-select">
            <el-option label="成功" value="success" />
            <el-option label="未命中" value="no_results" />
            <el-option label="失败" value="failed" />
            <el-option label="处理中" value="running" />
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

          <el-popover
            v-model:visible="advancedOpen"
            placement="bottom-start"
            :width="360"
            trigger="click"
          >
            <template #reference>
              <el-button class="advanced-btn">
                <el-icon><Filter /></el-icon>
                更多筛选
                <span v-if="advancedCount" class="advanced-count">{{ advancedCount }}</span>
              </el-button>
            </template>
            <div class="adv-panel">
              <div class="adv-head">
                <span>更多筛选</span>
                <button type="button" @click="resetAdvanced">清空条件</button>
              </div>
              <label class="adv-field">
                <span>复核状态</span>
                <el-select v-model="filters.reviewStatus" clearable placeholder="全部复核状态">
                  <el-option label="待补资料" value="pending" />
                  <el-option label="已补充" value="resolved" />
                  <el-option label="未复核" value="none" />
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
                <span>回答来源</span>
                <el-select v-model="filters.faqMatched" clearable placeholder="全部回答来源">
                  <el-option label="FAQ 直出" value="true" />
                  <el-option label="模型生成" value="false" />
                </el-select>
              </label>
              <div class="adv-field is-block">
                <span>时间范围</span>
                <el-date-picker
                  v-model="filters.timeRange"
                  type="datetimerange"
                  range-separator="至"
                  start-placeholder="开始时间"
                  end-placeholder="结束时间"
                  value-format="YYYY-MM-DDTHH:mm:ss"
                  class="adv-time"
                />
              </div>
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

      <el-table
        :data="rows"
        v-loading="loading"
        class="records-table"
        @row-click="openDetail"
      >
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="query" label="问题" min-width="240" show-overflow-tooltip />
        <el-table-column label="公司" width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ row.company_name ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="知识库" width="170" show-overflow-tooltip>
          <template #default="{ row }">{{ row.knowledge_base_name }}</template>
        </el-table-column>
        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <StatusPill
              :label="statusMeta[row.status]?.label ?? row.status"
              :tone="statusMeta[row.status]?.tone ?? 'info'"
            />
          </template>
        </el-table-column>
        <el-table-column label="复核" width="108">
          <template #default="{ row }">
            <StatusPill
              :label="reviewMeta[row.review_status]?.label ?? row.review_status"
              :tone="reviewMeta[row.review_status]?.tone ?? 'info'"
              :dot="row.review_status !== 'none'"
            />
          </template>
        </el-table-column>
        <el-table-column label="反馈" width="92">
          <template #default="{ row }">
            <StatusPill
              v-if="row.feedback"
              :label="feedbackMeta[row.feedback]?.label ?? row.feedback"
              :tone="feedbackMeta[row.feedback]?.tone ?? 'info'"
            />
            <span v-else class="cell-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="回答来源" width="110">
          <template #default="{ row }">
            <StatusPill
              :label="row.faq_matched ? 'FAQ 直出' : '模型生成'"
              :tone="row.faq_matched ? 'primary' : 'info'"
            />
          </template>
        </el-table-column>
        <el-table-column label="模式" width="80">
          <template #default="{ row }">{{ modeLabel[row.retrieval_mode] ?? row.retrieval_mode }}</template>
        </el-table-column>
        <el-table-column prop="result_count" label="结果数" width="80" align="center" />
        <el-table-column label="总耗时" width="100">
          <template #default="{ row }">{{ formatMs(row.total_duration_ms) }}</template>
        </el-table-column>
        <el-table-column label="回答模型" width="170" show-overflow-tooltip>
          <template #default="{ row }">{{ row.chat_model_name ?? '—' }}</template>
        </el-table-column>
        <template #empty>
          <EmptyState
            title="暂无问答记录"
            text="调整筛选条件，或等待新的提问进来后自动留档。"
            :muted="Boolean(filters.q || filters.status || filters.timeRange.length)"
          />
        </template>
      </el-table>

      <template #footer>
        <el-pagination
          v-model:current-page="pager.page"
          v-model:page-size="pager.pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          background
          layout="total, sizes, prev, pager, next"
          @current-change="loadRecords"
          @size-change="search"
        />
      </template>
    </DataPanel>

    <el-drawer v-model="drawer.open" size="640px" title="问答详情">
      <div v-loading="drawer.loading" class="drawer-body">        <template v-if="drawer.detail">
          <section class="block">
            <h4 class="block-title">问题</h4>
            <p class="query-text">{{ drawer.detail.query }}</p>
          </section>

          <section class="block">
            <h4 class="block-title">回答</h4>
            <div v-if="drawer.detail.answer" class="answer-text markdown-body" v-html="renderMarkdown(drawer.detail.answer)" />
            <el-alert
              v-else-if="drawer.detail.error_message"
              type="error"
              :closable="false"
              show-icon
              :title="drawer.detail.error_message"
            />
            <p v-else class="answer-text muted">暂无回答</p>
          </section>

          <section class="block">
            <h4 class="block-title">请求信息</h4>
            <div class="meta-grid">
              <div class="meta-item"><span class="meta-label">状态</span><span><StatusPill :label="statusMeta[drawer.detail.status]?.label ?? drawer.detail.status" :tone="statusMeta[drawer.detail.status]?.tone ?? 'info'" /></span></div>
              <div class="meta-item"><span class="meta-label">公司</span><span>{{ drawer.detail.company_name ?? '—' }}</span></div>
              <div class="meta-item"><span class="meta-label">知识库</span><span>{{ drawer.detail.knowledge_base_name }}</span></div>
              <div class="meta-item"><span class="meta-label">检索模式</span><span>{{ modeLabel[drawer.detail.retrieval_mode] ?? drawer.detail.retrieval_mode }}</span></div>
              <div class="meta-item"><span class="meta-label">Top K</span><span>{{ drawer.detail.top_k }}</span></div>
              <div class="meta-item"><span class="meta-label">结果数</span><span>{{ drawer.detail.result_count }}</span></div>
              <div class="meta-item"><span class="meta-label">回答来源</span><span>{{ drawer.detail.faq_matched ? 'FAQ 直出' : '模型生成' }}</span></div>
              <div class="meta-item"><span class="meta-label">用户反馈</span><span>
                <StatusPill v-if="drawer.detail.feedback" :label="feedbackMeta[drawer.detail.feedback]?.label ?? drawer.detail.feedback" :tone="feedbackMeta[drawer.detail.feedback]?.tone ?? 'info'" />
                <span v-else>—</span>
              </span></div>
              <div class="meta-item"><span class="meta-label">反馈时间</span><span>{{ drawer.detail.feedback_at ? formatDateTime(drawer.detail.feedback_at) : '—' }}</span></div>
              <div v-if="drawer.detail.feedback_reason" class="meta-item meta-wide"><span class="meta-label">反馈原因</span><span>{{ drawer.detail.feedback_reason }}</span></div>
              <div class="meta-item"><span class="meta-label">检索耗时</span><span>{{ formatMs(drawer.detail.retrieval_duration_ms) }}</span></div>
              <div class="meta-item"><span class="meta-label">生成耗时</span><span>{{ formatMs(drawer.detail.generation_duration_ms) }}</span></div>
              <div class="meta-item"><span class="meta-label">总耗时</span><span>{{ formatMs(drawer.detail.total_duration_ms) }}</span></div>
              <div class="meta-item"><span class="meta-label">总 Token</span><span>{{ drawer.detail.total_tokens?.toLocaleString('zh-CN') ?? '—' }}</span></div>
              <div class="meta-item meta-wide"><span class="meta-label">Token 明细</span><span>{{ drawer.detail.prompt_tokens == null && drawer.detail.completion_tokens == null ? '—' : `输入 ${drawer.detail.prompt_tokens?.toLocaleString('zh-CN') ?? '—'} · 输出 ${drawer.detail.completion_tokens?.toLocaleString('zh-CN') ?? '—'}` }}</span></div>
              <div class="meta-item"><span class="meta-label">回答模型</span><span>{{ drawer.detail.chat_model_name ?? '—' }}</span></div>
              <div class="meta-item"><span class="meta-label">时间</span><span>{{ formatDateTime(drawer.detail.created_at) }}</span></div>
              <div class="meta-item meta-wide"><span class="meta-label">请求 ID</span><span class="mono">{{ drawer.detail.request_id }}</span></div>
            </div>
          </section>

          <section v-if="drawer.detail.sources.length" class="block">
            <h4 class="block-title">来源快照（{{ drawer.detail.sources.length }}）</h4>
            <div v-for="source in drawer.detail.sources" :key="source.rank" class="source-card">
              <div class="source-head">
                <span class="rank-badge">{{ source.rank }}</span>
                <span class="source-name">{{ source.filename }} <em>v{{ source.version_number }}</em></span>
                <span v-if="source.page_number" class="source-meta">第 {{ source.page_number }} 页</span>
                <span v-if="source.section" class="source-meta">{{ source.section }}</span>
              </div>
              <div class="score-row">
                <span class="score-label">rerank</span>
                <span class="score-bar"><i :class="['score-fill', scoreTier(source.rerank_score)]" :style="{ width: `${Math.round((source.rerank_score ?? 0) * 100)}%` }" /></span>
                <span class="score-value">{{ formatScore(source.rerank_score) }}</span>
                <span class="score-label">bm25</span>
                <span class="score-value">{{ formatScore(source.bm25_score) }}</span>
                <span class="score-label">rrf</span>
                <span class="score-value">{{ formatScore(source.rrf_score) }}</span>
                <span class="score-label">cosine</span>
                <span class="score-value">{{ formatScore(source.cosine_distance) }}</span>
              </div>
              <p class="snapshot-text markdown-body" v-html="renderMarkdown(source.content_snapshot)" />
            </div>
          </section>

          <section v-if="drawer.detail.events.length" class="block">
            <h4 class="block-title">阶段日志</h4>
            <div v-for="event in drawer.detail.events" :key="event.created_at + event.stage" class="event-row">
              <span class="event-time">{{ formatDateTime(event.created_at) }}</span>
              <StatusPill
                :label="event.status"
                :tone="event.status === 'success' ? 'success' : event.status === 'failed' ? 'danger' : 'info'"
              />
              <span class="event-stage">{{ stageLabel[event.stage] ?? event.stage }}</span>
              <span class="event-duration">{{ formatMs(event.duration_ms) }}</span>
              <span v-if="event.message || event.error_code" class="event-message">{{ event.error_code ? `${event.error_code}：` : '' }}{{ event.message }}</span>
            </div>
          </section>
        </template>
      </div>
      <template v-if="drawer.detail" #footer>
        <div class="drawer-footer">
          <span class="review-hint">
            复核状态
            <StatusPill
              :label="reviewMeta[drawer.detail.review_status]?.label ?? drawer.detail.review_status"
              :tone="reviewMeta[drawer.detail.review_status]?.tone ?? 'info'"
            />
          </span>
          <el-button v-if="canWrite && drawer.detail.answer" @click="openFaqDialog">沉淀为 FAQ</el-button>
          <el-button
            v-if="canWrite"
            :type="drawer.detail.review_status === 'resolved' ? 'default' : 'primary'"
            @click="toggleReview"
          >
            {{ drawer.detail.review_status === 'resolved' ? '重新打开复核' : '标记已补充' }}
          </el-button>
        </div>
      </template>
    </el-drawer>

    <el-dialog v-model="faqDialog.open" title="沉淀为 FAQ" width="560px">
      <p class="faq-tip">保存后该问题会优先按 FAQ 直出，并自动把这条记录标记为已补充。</p>
      <el-form label-position="top">
        <el-form-item label="问题">
          <el-input v-model="faqDialog.question" maxlength="1000" show-word-limit />
        </el-form-item>
        <el-form-item label="答案">
          <el-input
            v-model="faqDialog.answer"
            type="textarea"
            :rows="7"
            maxlength="20000"
            placeholder="可基于模型回答再校正一遍"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="faqDialog.open = false">取消</el-button>
        <el-button type="primary" :loading="faqDialog.submitting" @click="submitFaq">
          保存 FAQ
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
.panel-toolbar {
  padding: 14px 18px 0;
}
.cell-muted {
  color: var(--el-text-color-placeholder);
}
.faq-tip {
  margin: 0 0 14px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.company-select {
  width: 150px;
}
.kb-select {
  width: 160px;
}
.status-select {
  width: 120px;
}
.q-input {
  width: 210px;
}
.advanced-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.advanced-count {
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 999px;
  background: var(--el-color-primary);
  color: #fff;
  font-size: 10px;
  line-height: 16px;
  text-align: center;
}
.adv-panel {
  padding: 2px 2px 0;
}
.adv-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
}
.adv-head button {
  border: 0;
  background: none;
  padding: 0;
  font-size: var(--kb-font-xs);
  color: var(--el-color-primary);
  cursor: pointer;
}
.adv-field {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-regular);
}
.adv-field > span {
  flex: none;
  width: 56px;
}
.adv-field .el-select {
  flex: 1 1 auto;
}
.adv-field.is-block {
  display: block;
}
.adv-field.is-block > span {
  display: block;
  width: auto;
  margin-bottom: 6px;
}
.adv-time {
  width: 100%;
}
.adv-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 10px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.records-table :deep(.el-table__row) {
  cursor: pointer;
}
.drawer-body {
  min-height: 320px;
}
.block {
  margin-bottom: 22px;
}
.block-title {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 700;
  color: var(--el-text-color-primary);
  letter-spacing: 0.5px;
}
.block-title::before {
  content: '';
  display: inline-block;
  width: 4px;
  height: 14px;
  border-radius: 999px;
  background: var(--el-color-primary);
  margin-right: 8px;
  vertical-align: -2px;
}
.query-text {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  line-height: 1.7;
}
.answer-text {
  margin: 0;
  line-height: 1.8;
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--el-border-radius-base);
  padding: 14px 16px;
  overflow-x: auto;
}
.answer-text.muted {
  color: var(--el-text-color-secondary);
}
.meta-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px 16px;
}
.meta-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  min-width: 0;
}
.meta-item span:last-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.meta-wide {
  grid-column: 1 / -1;
}
.meta-label {
  flex-shrink: 0;
  color: var(--el-text-color-secondary);
}
.mono {
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 12px;
}
.source-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--el-border-radius-base);
  padding: 12px 14px;
  margin-bottom: 10px;
  background: #fff;
}
.source-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.rank-badge {
  width: 22px;
  height: 22px;
  border-radius: 7px;
  display: grid;
  place-items: center;
  font-size: 12px;
  font-weight: 700;
  color: #fff;
  background: var(--el-color-primary);
  flex-shrink: 0;
}
.source-name {
  font-weight: 600;
  font-size: 13px;
}
.source-name em {
  font-style: normal;
  color: var(--el-text-color-secondary);
  font-weight: 400;
}
.source-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color);
  border-radius: 4px;
  padding: 2px 8px;
}
.score-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  font-size: 12px;
}
.score-label {
  color: var(--el-text-color-secondary);
  flex-shrink: 0;
}
.score-label:first-of-type {
  margin-left: 2px;
}
.score-bar {
  width: 90px;
  height: 6px;
  border-radius: 999px;
  background: var(--el-fill-color);
  overflow: hidden;
  flex-shrink: 0;
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
.score-value {
  font-family: 'Cascadia Mono', Consolas, monospace;
  color: var(--el-text-color-regular);
  min-width: 52px;
}
.snapshot-text {
  margin: 10px 0 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--el-text-color-regular);
  max-height: 160px;
  overflow-y: auto;
  overflow-x: auto;
  background: var(--el-fill-color-lighter);
  border-radius: var(--el-border-radius-small);
  padding: 10px 12px;
}
.event-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 8px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
  font-size: 13px;
}
.event-row:last-child {
  border-bottom: none;
}
.event-time {
  color: var(--el-text-color-secondary);
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 12px;
}
.event-stage {
  font-weight: 600;
}
.event-duration {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.event-message {
  flex-basis: 100%;
  color: var(--el-text-color-secondary);
  padding-left: 2px;
}
.drawer-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.review-hint {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
/* 答案与来源快照由 v-html 注入，元素不带 scoped 属性，需 :deep() 穿透。 */
:deep(.markdown-body p) {
  margin: 4px 0;
  line-height: 1.7;
}
:deep(.markdown-body h3),
:deep(.markdown-body h4),
:deep(.markdown-body h5),
:deep(.markdown-body h6) {
  margin: 10px 0 4px;
  font-size: 15px;
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
  font-size: 12px;
  color: var(--el-color-primary-dark-2);
}
:deep(.markdown-body img.md-image) {
  display: inline-block;
  max-width: 100%;
  max-height: 260px;
  border-radius: var(--el-border-radius-base);
  border: 1px solid var(--el-border-color-lighter);
  margin: 6px 4px 2px 0;
  vertical-align: middle;
}
:deep(.markdown-body table) {
  border-collapse: collapse;
  margin: 8px 0;
  width: 100%;
  font-size: 13px;
}
:deep(.markdown-body th),
:deep(.markdown-body td) {
  border: 1px solid var(--el-border-color-lighter);
  padding: 6px 10px;
  text-align: left;
  vertical-align: top;
  word-break: break-word;
}
:deep(.markdown-body th) {
  background: var(--el-fill-color-light);
  font-weight: 600;
}
:deep(.markdown-body td img.md-image) {
  max-height: 120px;
  margin: 2px 4px 2px 0;
}
</style>
