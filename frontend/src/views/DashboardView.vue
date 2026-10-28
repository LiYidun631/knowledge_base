<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { client, errorMessage } from '../api/client'

interface DailyTrendPoint {
  date: string
  qa_total: number
  qa_success: number
  avg_total_duration_ms: number | null
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

interface TokenUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  recent_prompt_tokens: number
  recent_completion_tokens: number
  recent_total_tokens: number
  measured_qa_count: number
}

interface LatencyStats {
  sample_count: number
  retrieval_p50_ms: number | null
  retrieval_p90_ms: number | null
  generation_p50_ms: number | null
  generation_p90_ms: number | null
  total_p50_ms: number | null
  total_p90_ms: number | null
  total_max_ms: number | null
}

interface LatencyBreakdown {
  key: string
  label: string
  qa_total: number
  qa_failed: number
  failure_rate: number
  avg_total_duration_ms: number | null
  p90_total_duration_ms: number | null
}

interface RecentStatistics {
  qa_total: number
  qa_success: number
  qa_no_results: number
  qa_failed: number
  failure_rate: number
  no_result_rate: number
  latency: LatencyStats
  models: LatencyBreakdown[]
  knowledge_bases: LatencyBreakdown[]
}

interface ModelCost {
  model_name: string
  provider: string | null
  qa_count: number
  prompt_tokens: number
  completion_tokens: number
  input_cost: number
  output_cost: number
  total_cost: number
  priced: boolean
}

interface CostBlock {
  currency: string
  recent_cost: number
  total_cost: number
  recent_priced_tokens: number
  recent_unpriced_tokens: number
  unpriced_models: string[]
  models: ModelCost[]
}

interface Statistics {
  companies_total: number
  knowledge_bases_total: number
  documents_total: number
  documents_by_status: Record<string, number>
  qa_total: number
  qa_by_status: Record<string, number>
  review_pending: number
  token_usage: TokenUsage
  // 旧后端没有该字段，缺失时整块隐藏而不是报错。
  recent?: RecentStatistics
  cost?: CostBlock
  range?: { start: string; end: string; days: number }
  daily_trend: DailyTrendPoint[]
}

const router = useRouter()
const loading = ref(false)
const stats = ref<Statistics | null>(null)
const animated = reactive({ kbs: 0, docs: 0, qa: 0, pending: 0 })

function toIsoDate(value: Date): string {
  // 用本地时区拼日期，避免 toISOString 的 UTC 偏移把日期推前一天。
  const year = value.getFullYear()
  const month = `${value.getMonth() + 1}`.padStart(2, '0')
  const day = `${value.getDate()}`.padStart(2, '0')
  return `${year}-${month}-${day}`
}

function daysAgo(days: number): Date {
  const value = new Date()
  value.setDate(value.getDate() - days)
  return value
}

const range = ref<[string, string] | null>([
  toIsoDate(daysAgo(13)),
  toIsoDate(new Date()),
])

const rangeShortcuts = [
  { text: '近 7 天', value: () => [daysAgo(6), new Date()] },
  { text: '近 14 天', value: () => [daysAgo(13), new Date()] },
  { text: '近 30 天', value: () => [daysAgo(29), new Date()] },
  { text: '近 90 天', value: () => [daysAgo(89), new Date()] },
]

// 与后端上限对齐：不选未来日期，最多回溯 366 天。
function disabledDate(value: Date): boolean {
  const latest = new Date()
  latest.setHours(23, 59, 59, 999)
  const earliest = daysAgo(365)
  earliest.setHours(0, 0, 0, 0)
  return value.getTime() > latest.getTime() || value.getTime() < earliest.getTime()
}

const rangeLabel = computed(() => {
  const days = stats.value?.range?.days
  const start = stats.value?.range?.start
  const end = stats.value?.range?.end
  if (!days || !start || !end) return '最近 14 天'
  if (days === 1) return `${start} 单日`
  if (start === toIsoDate(daysAgo(days - 1)) && end === toIsoDate(new Date())) {
    return `最近 ${days} 天`
  }
  return `${start.slice(5)} 至 ${end.slice(5)}`
})

async function load() {
  if (!range.value) return
  loading.value = true
  try {
    const { data } = await client.get<Statistics>('/admin/statistics', {
      params: { start: range.value[0], end: range.value[1] },
    })
    stats.value = data
    animateNumbers()
  } catch (error) {
    errorMessage(error, '加载统计数据失败')
  } finally {
    loading.value = false
  }
}

function animateNumbers() {
  if (!stats.value) return
  const targets = {
    kbs: stats.value.knowledge_bases_total,
    docs: stats.value.documents_total,
    qa: stats.value.qa_total,
    pending: stats.value.review_pending,
  }
  const duration = 700
  const start = performance.now()
  function tick(now: number) {
    const progress = Math.min(1, (now - start) / duration)
    const eased = 1 - Math.pow(1 - progress, 3)
    animated.kbs = Math.round(targets.kbs * eased)
    animated.docs = Math.round(targets.docs * eased)
    animated.qa = Math.round(targets.qa * eased)
    animated.pending = Math.round(targets.pending * eased)
    if (progress < 1) requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
  // 后台标签页 rAF 会被节流，兜底直接落到目标值。
  window.setTimeout(() => {
    animated.kbs = targets.kbs
    animated.docs = targets.docs
    animated.qa = targets.qa
    animated.pending = targets.pending
  }, duration + 120)
}

const successRate = computed(() => {
  if (!stats.value || stats.value.qa_total === 0) return null
  return Math.round(((stats.value.qa_by_status['success'] ?? 0) / stats.value.qa_total) * 100)
})

const avgDurationMs = computed(() => {
  const values = (stats.value?.daily_trend ?? [])
    .map((point) => point.avg_total_duration_ms)
    .filter((value): value is number => value != null)
  if (values.length === 0) return null
  return Math.round(values.reduce((sum, value) => sum + value, 0) / values.length)
})

function formatDuration(ms: number | null): string {
  if (ms == null) return '—'
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`
}

function formatTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`
  if (value >= 10_000) return `${(value / 10_000).toFixed(1)} 万`
  return value.toLocaleString('zh-CN')
}

// 输入/输出占比按累计用量计算，用于展示成本结构。
const tokenShare = computed(() => {
  const usage = stats.value?.token_usage
  if (!usage || usage.total_tokens === 0) return null
  return {
    prompt: Math.round((usage.prompt_tokens / usage.total_tokens) * 100),
    completion: Math.round((usage.completion_tokens / usage.total_tokens) * 100),
  }
})

const qaStatusRows = computed(() => {
  const byStatus = stats.value?.qa_by_status ?? {}
  const total = stats.value?.qa_total ?? 0
  const meta: Record<string, { label: string; color: string }> = {
    success: { label: '成功', color: '#16a34a' },
    no_results: { label: '未命中', color: '#d97706' },
    failed: { label: '失败', color: '#dc2626' },
    running: { label: '处理中', color: '#8a8f99' },
  }
  return Object.entries(meta).map(([status, item]) => {
    const count = byStatus[status] ?? 0
    return {
      status,
      label: item.label,
      color: item.color,
      count,
      percent: total === 0 ? 0 : Math.round((count / total) * 100),
    }
  })
})

const docStatusRows = computed(() => {
  const byStatus = stats.value?.documents_by_status ?? {}
  const meta: Record<string, { label: string; color: string }> = {
    completed: { label: '已完成', color: '#16a34a' },
    processing: { label: '处理中', color: '#d97706' },
    failed: { label: '失败', color: '#dc2626' },
    queued: { label: '排队中', color: '#8a8f99' },
    deleting: { label: '删除中', color: '#8a8f99' },
  }
  return Object.entries(meta)
    .map(([status, item]) => ({ status, ...item, count: byStatus[status] ?? 0 }))
    .filter((row) => row.count > 0)
})

// 近 14 天质量与耗时：对齐后端 recent 区块，旧后端缺失时不展示。
const recent = computed(() => stats.value?.recent ?? null)
// 成本核算只统计生成阶段 token，旧后端缺失时隐藏。
const cost = computed(() => stats.value?.cost ?? null)
const groupMode = ref<'models' | 'knowledge_bases'>('models')

const costByModel = computed(() => {
  const map = new Map<string, ModelCost>()
  for (const item of cost.value?.models ?? []) map.set(item.model_name, item)
  return map
})

function formatPercent(value: number): string {
  return `${Number(value.toFixed(2))}%`
}

function formatCost(value: number): string {
  if (value === 0) return '0'
  // 单条问答费用常在分以下，保留 4 位小数避免显示成 0.00。
  return value < 1 ? value.toFixed(4) : value.toFixed(2)
}

function openPricing() {
  router.push({ path: '/models', query: { tab: 'prices' } })
}

// 三个阶段共用一条刻度（以总耗时最大值为基准），便于横向比较；FAQ 直出已由后端剔除。
const latencyStages = computed(() => {
  const latency = recent.value?.latency
  if (!latency) return []
  const stages = [
    { key: 'retrieval', label: '检索', p50: latency.retrieval_p50_ms, p90: latency.retrieval_p90_ms },
    { key: 'generation', label: '生成', p50: latency.generation_p50_ms, p90: latency.generation_p90_ms },
    { key: 'total', label: '总耗时', p50: latency.total_p50_ms, p90: latency.total_p90_ms },
  ]
  const scale = Math.max(latency.total_max_ms ?? 0, ...stages.map((stage) => stage.p90 ?? 0), 1)
  return stages.map((stage) => ({
    ...stage,
    p50Percent: stage.p50 == null ? 0 : Math.min(100, (stage.p50 / scale) * 100),
    p90Percent: stage.p90 == null ? 0 : Math.min(100, (stage.p90 / scale) * 100),
  }))
})

const hasLatency = computed(() =>
  latencyStages.value.some((stage) => stage.p50 != null || stage.p90 != null),
)

const groupRows = computed(() => {
  const groups =
    (groupMode.value === 'models' ? recent.value?.models : recent.value?.knowledge_bases) ?? []
  const scale = Math.max(1, ...groups.map((group) => group.p90_total_duration_ms ?? 0))
  return groups.map((group) => {
    // 按模型分组时顺带展示窗口费用；未定价模型显示为 null。
    const costItem = groupMode.value === 'models' ? costByModel.value.get(group.key) : undefined
    return {
      ...group,
      p90Percent:
        group.p90_total_duration_ms == null
          ? 0
          : Math.min(100, (group.p90_total_duration_ms / scale) * 100),
      cost: costItem?.priced ? costItem.total_cost : null,
    }
  })
})

// 趋势图：柱体高度表示问答量，深色段为成功数，折线再叠加一条成功数走势。
const trendChart = computed(() => {
  const trend = stats.value?.daily_trend ?? []
  const width = 720
  const height = 264
  const plotTop = 22
  const plotBottom = 222
  const plotLeft = 46
  const plotRight = 706
  const plotHeight = plotBottom - plotTop
  const slot = trend.length > 0 ? (plotRight - plotLeft) / trend.length : 0
  const barWidth = Math.max(3, Math.min(26, slot * 0.56))
  const max = Math.max(1, ...trend.map((point) => point.qa_total))
  // 日期标签最多 8 个，窗口变宽时自动抽稀避免叠字。
  const labelStep = Math.max(1, Math.ceil(trend.length / 8))
  const points = trend.map((point, index) => {
    const x = plotLeft + index * slot + (slot - barWidth) / 2
    const totalHeight = (point.qa_total / max) * plotHeight
    const successHeight = (point.qa_success / max) * plotHeight
    return {
      ...point,
      index,
      barX: x,
      slotX: plotLeft + index * slot,
      centerX: x + barWidth / 2,
      barWidth,
      totalY: plotBottom - totalHeight,
      totalHeight,
      successY: plotBottom - successHeight,
      successHeight,
      showLabel: index % labelStep === 0 || index === trend.length - 1,
    }
  })
  const successPoints = points.map((point) => `${point.centerX},${point.successY}`).join(' ')
  return {
    trend,
    points,
    successPoints,
    width,
    height,
    plotTop,
    plotBottom,
    plotLeft,
    plotRight,
    plotHeight,
    slot,
    max,
  }
})

const hoverIndex = ref<number | null>(null)

const hoverPoint = computed(() => {
  if (hoverIndex.value == null) return null
  const chartValue = trendChart.value
  const point = chartValue.points[hoverIndex.value]
  if (!point) return null
  // 提示卡跟随柱顶，靠边时夹紧到图内，避免溢出面板。
  const left = Math.min(92, Math.max(8, (point.centerX / chartValue.width) * 100))
  const topPercent = (point.totalY / chartValue.height) * 100
  return {
    ...point,
    leftPercent: left,
    topPercent,
    // 柱子太高时把提示卡翻到柱顶下方，别顶穿卡片标题。
    placement: topPercent < 32 ? 'below' : 'above',
  }
})

function enterBar(index: number) {
  hoverIndex.value = index
}

function leaveBars() {
  hoverIndex.value = null
}

// token 迷你柱：每天一根，深色为输出、浅色为输入。
const tokenBars = computed(() => {
  const trend = stats.value?.daily_trend ?? []
  const max = Math.max(1, ...trend.map((point) => point.total_tokens))
  return trend.map((point) => {
    // 旧后端可能没有输入/输出拆分，缺失时整根按输入展示。
    const completion = point.completion_tokens ?? 0
    return {
      date: point.date,
      total: point.total_tokens,
      height: Math.max(3, (point.total_tokens / max) * 100),
      completionShare: point.total_tokens === 0 ? 0 : (completion / point.total_tokens) * 100,
    }
  })
})

function openPending() {
  router.push({ path: '/qa-records', query: { review: 'pending' } })
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="dashboard">
    <template v-if="stats">
      <div class="page-head">
        <div>
          <div class="page-title">统计概览</div>
          <div class="page-desc">{{ rangeLabel }} · 关键指标与问答表现</div>
        </div>
        <div class="head-controls">
          <el-date-picker
            v-model="range"
            type="daterange"
            size="small"
            unlink-panels
            :clearable="false"
            :shortcuts="rangeShortcuts"
            :disabled-date="disabledDate"
            value-format="YYYY-MM-DD"
            range-separator="→"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            class="range-picker"
            @change="load"
          />
          <el-button :icon="Refresh" circle aria-label="刷新统计数据" @click="load" />
        </div>
      </div>

      <section class="stat-cards">
        <article class="stat-card" style="--accent: #4f46e5; --delay: 0ms">
          <div class="stat-icon"><el-icon><Collection /></el-icon></div>
          <div class="stat-body">
            <div class="stat-value">{{ animated.kbs }}</div>
            <div class="stat-label">活跃知识库</div>
            <div class="stat-sub">活跃公司 {{ stats.companies_total }} 家</div>
          </div>
        </article>
        <article class="stat-card" style="--accent: #3b82f6; --delay: 40ms">
          <div class="stat-icon"><el-icon><Document /></el-icon></div>
          <div class="stat-body">
            <div class="stat-value">{{ animated.docs }}</div>
            <div class="stat-label">文档总数</div>
            <div class="stat-sub">
              处理中 {{ stats.documents_by_status['processing'] ?? 0 }} ·
              失败 {{ stats.documents_by_status['failed'] ?? 0 }}
            </div>
          </div>
        </article>
        <article class="stat-card" style="--accent: #0ea5e9; --delay: 80ms">
          <div class="stat-icon"><el-icon><ChatDotRound /></el-icon></div>
          <div class="stat-body">
            <div class="stat-value">{{ animated.qa }}</div>
            <div class="stat-label">问答总量</div>
            <div class="stat-sub">
              成功率 {{ successRate == null ? '—' : successRate + '%' }} ·
              平均耗时 {{ formatDuration(avgDurationMs) }}
            </div>
          </div>
        </article>
        <article
          class="stat-card pending"
          style="--accent: #d97706; --delay: 120ms"
          role="button"
          tabindex="0"
          @click="openPending"
          @keydown.enter="openPending"
        >
          <div class="stat-icon"><el-icon><WarningFilled /></el-icon></div>
          <div class="stat-body">
            <div class="stat-value">{{ animated.pending }}</div>
            <div class="stat-label">待补资料</div>
            <div class="stat-sub">点击查看未命中 / 低分问答</div>
          </div>
        </article>
      </section>

      <section class="panels">
        <el-card class="panel trend-panel">
          <template #header>
            <div class="panel-head">
              <span>{{ rangeLabel }}问答趋势</span>
              <span class="panel-hint">柱高为问答量 · 折线为成功数</span>
            </div>
          </template>
          <div
            class="chart-wrap"
            @mouseleave="leaveBars"
          >
            <svg
              :key="`${stats.range?.start}-${stats.range?.end}`"
              class="trend-chart"
              :viewBox="`0 0 ${trendChart.width} ${trendChart.height}`"
              role="img"
              :aria-label="`${rangeLabel}问答量趋势，柱高表示问答量，折线表示成功数`"
            >
              <defs>
                <linearGradient id="trend-bar-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stop-color="#c7d2fe" />
                  <stop offset="100%" stop-color="#e4e9fd" />
                </linearGradient>
              </defs>
              <g v-for="fraction in [0, 0.25, 0.5, 0.75, 1]" :key="fraction">
                <line
                  :x1="trendChart.plotLeft"
                  :x2="trendChart.plotRight"
                  :y1="trendChart.plotBottom - fraction * trendChart.plotHeight"
                  :y2="trendChart.plotBottom - fraction * trendChart.plotHeight"
                  stroke="#eef0f3"
                  stroke-width="1"
                />
                <text
                  :x="trendChart.plotLeft - 8"
                  :y="trendChart.plotBottom - fraction * trendChart.plotHeight + 4"
                  text-anchor="end"
                  class="chart-grid-label"
                >
                  {{ Math.round(trendChart.max * fraction) }}
                </text>
              </g>

              <g v-for="(point, index) in trendChart.points" :key="point.date">
                <g class="chart-bar" :class="{ 'is-dim': hoverIndex !== null && hoverIndex !== index }">
                  <rect
                    class="bar-total"
                    :x="point.barX"
                    :y="point.totalY"
                    :width="point.barWidth"
                    :height="Math.max(point.totalHeight, point.qa_total > 0 ? 2 : 0)"
                    rx="3"
                    fill="url(#trend-bar-fill)"
                    :style="{ '--i': Math.min(index, 26) }"
                  />
                  <rect
                    v-if="point.qa_success > 0"
                    class="bar-success"
                    :x="point.barX"
                    :y="point.successY"
                    :width="point.barWidth"
                    :height="Math.max(point.successHeight, 2)"
                    rx="3"
                    fill="#4f46e5"
                    :style="{ '--i': Math.min(index, 26) }"
                  />
                </g>
                <rect
                  class="chart-hit"
                  :x="point.slotX"
                  :y="trendChart.plotTop"
                  :width="trendChart.slot"
                  :height="trendChart.plotHeight"
                  @mouseenter="enterBar(index)"
                />
              </g>

              <line
                v-if="hoverPoint"
                class="chart-guide"
                :x1="hoverPoint.centerX"
                :x2="hoverPoint.centerX"
                :y1="trendChart.plotTop"
                :y2="trendChart.plotBottom"
              />
              <polyline
                v-if="trendChart.points.length > 1"
                class="success-line"
                :points="trendChart.successPoints"
                pathLength="1"
                fill="none"
              />
              <circle
                v-for="point in trendChart.points.filter((item) => item.qa_success > 0)"
                :key="`dot-${point.date}`"
                class="success-dot"
                :cx="point.centerX"
                :cy="point.successY"
                r="2.6"
              />
              <g v-for="point in trendChart.points" :key="`label-${point.date}`">
                <text
                  v-if="point.showLabel"
                  :x="point.centerX"
                  :y="trendChart.plotBottom + 18"
                  text-anchor="middle"
                  class="chart-date-label"
                >
                  {{ point.date.slice(5) }}
                </text>
              </g>
            </svg>

            <div
              v-if="hoverPoint"
              class="chart-tip"
              :class="`is-${hoverPoint.placement}`"
              :style="{ left: hoverPoint.leftPercent + '%', top: hoverPoint.topPercent + '%' }"
            >
              <div class="tip-date">{{ hoverPoint.date }}</div>
              <div class="tip-row"><span>问答</span><b>{{ hoverPoint.qa_total }}</b></div>
              <div class="tip-row"><span>成功</span><b>{{ hoverPoint.qa_success }}</b></div>
              <div class="tip-row">
                <span>平均耗时</span><b>{{ formatDuration(hoverPoint.avg_total_duration_ms) }}</b>
              </div>
              <div class="tip-row"><span>tokens</span><b>{{ formatTokens(hoverPoint.total_tokens) }}</b></div>
            </div>
          </div>
        </el-card>

        <el-card class="panel status-panel">
          <template #header>
            <div class="panel-head">
              <span>问答状态分布</span>
              <span class="panel-hint">共 {{ stats.qa_total }} 条</span>
            </div>
          </template>
          <div class="qa-status-list">
            <div v-for="row in qaStatusRows" :key="row.status" class="qa-status-row">
              <span class="qa-status-label">{{ row.label }}</span>
              <div class="qa-status-track">
                <div
                  class="qa-status-fill"
                  :style="{ width: row.percent + '%', background: row.color }"
                ></div>
              </div>
              <span class="qa-status-count">{{ row.count }} · {{ row.percent }}%</span>
            </div>
          </div>
          <div class="doc-section">
            <div class="doc-section-title">文档处理状态</div>
            <div class="doc-chips">
              <span
                v-for="row in docStatusRows"
                :key="row.status"
                class="doc-chip"
                :style="{ color: row.color, borderColor: row.color + '55', background: row.color + '14' }"
              >
                {{ row.label }} {{ row.count }}
              </span>
              <span v-if="docStatusRows.length === 0" class="doc-chip empty">暂无文档</span>
            </div>
          </div>
          <div class="token-section">
            <div class="token-head">
              <span class="token-title">Token 消耗</span>
              <span class="token-hint">
                {{ stats.token_usage.measured_qa_count }} 条已记录 · {{ rangeLabel }}
                {{ formatTokens(stats.token_usage.recent_total_tokens) }}
              </span>
            </div>
            <div class="token-total">
              <span class="token-number">{{ formatTokens(stats.token_usage.total_tokens) }}</span>
              <span class="token-unit">累计 tokens</span>
            </div>
            <template v-if="tokenShare">
              <div class="token-split-bar" aria-hidden="true">
                <span class="token-split-prompt" :style="{ width: tokenShare.prompt + '%' }"></span>
                <span class="token-split-completion" :style="{ width: tokenShare.completion + '%' }"></span>
              </div>
              <div class="token-chips">
                <span class="token-chip prompt">
                  输入 {{ formatTokens(stats.token_usage.prompt_tokens) }} · {{ tokenShare.prompt }}%
                </span>
                <span class="token-chip completion">
                  输出 {{ formatTokens(stats.token_usage.completion_tokens) }} ·
                  {{ tokenShare.completion }}%
                </span>
              </div>
              <div class="token-spark" aria-hidden="true">
                <span
                  v-for="(bar, index) in tokenBars"
                  :key="bar.date"
                  class="token-bar"
                  :style="{ '--h': bar.height + '%', '--i': Math.min(index, 26) }"
                  :title="`${bar.date} · ${formatTokens(bar.total)} tokens`"
                >
                  <i class="seg-completion" :style="{ height: bar.completionShare + '%' }"></i>
                  <i class="seg-prompt"></i>
                </span>
              </div>
            </template>
            <div v-else class="token-empty">暂无用量数据；FAQ 直出与历史记录不计入。</div>
            <div v-if="cost" class="cost-line">
              <span class="cost-main">
                <em>{{ rangeLabel }}成本</em>
                <b>¥{{ formatCost(cost.recent_cost) }}</b>
              </span>
              <span class="cost-sub">累计 ¥{{ formatCost(cost.total_cost) }}</span>
              <el-button link type="primary" size="small" class="cost-link" @click="openPricing">
                配置单价
              </el-button>
            </div>
            <div v-if="cost && cost.recent_unpriced_tokens > 0" class="cost-warn">
              {{ cost.unpriced_models.join('、') }} 未配置单价，
              {{ formatTokens(cost.recent_unpriced_tokens) }} tokens 未计入金额
            </div>
          </div>
        </el-card>
      </section>

      <template v-if="recent">
        <section class="quality-strip">
          <article class="quality-tile" style="--accent: #dc2626; --delay: 0ms">
            <span class="quality-label">失败率</span>
            <span class="quality-value">{{ formatPercent(recent.failure_rate) }}</span>
            <span class="quality-sub">失败 {{ recent.qa_failed }} / 共 {{ recent.qa_total }} 条</span>
          </article>
          <article class="quality-tile" style="--accent: #d97706; --delay: 40ms">
            <span class="quality-label">未命中率</span>
            <span class="quality-value">{{ formatPercent(recent.no_result_rate) }}</span>
            <span class="quality-sub">
              未命中 {{ recent.qa_no_results }} / 共 {{ recent.qa_total }} 条
            </span>
          </article>
          <article class="quality-tile" style="--accent: #6366f1; --delay: 80ms">
            <span class="quality-label">检索 P90</span>
            <span class="quality-value">{{ formatDuration(recent.latency.retrieval_p90_ms) }}</span>
            <span class="quality-sub">P50 {{ formatDuration(recent.latency.retrieval_p50_ms) }}</span>
          </article>
          <article class="quality-tile" style="--accent: #4f46e5; --delay: 120ms">
            <span class="quality-label">总耗时 P90</span>
            <span class="quality-value">{{ formatDuration(recent.latency.total_p90_ms) }}</span>
            <span class="quality-sub">
              P50 {{ formatDuration(recent.latency.total_p50_ms) }} · 最大
              {{ formatDuration(recent.latency.total_max_ms) }}
            </span>
          </article>
        </section>

        <section class="panels insights">
          <el-card class="panel">
            <template #header>
              <div class="panel-head">
                <span>耗时阶段分布</span>
                <span class="panel-hint">RAG 样本 {{ recent.latency.sample_count }} 条 · 不含 FAQ 直出</span>
              </div>
            </template>
            <div v-if="hasLatency" class="stage-list">
              <div v-for="stage in latencyStages" :key="stage.key" class="stage-row">
                <div class="stage-head">
                  <span class="stage-name">{{ stage.label }}</span>
                  <span class="stage-nums">
                    <span class="stage-num"><em>P50</em>{{ formatDuration(stage.p50) }}</span>
                    <span class="stage-num p90"><em>P90</em>{{ formatDuration(stage.p90) }}</span>
                  </span>
                </div>
                <div class="stage-track">
                  <div class="stage-fill" :style="{ width: stage.p90Percent + '%' }"></div>
                  <span
                    v-if="stage.p50 != null"
                    class="stage-mark"
                    :style="{ left: stage.p50Percent + '%' }"
                  ></span>
                </div>
              </div>
            </div>
            <div v-else class="panel-empty">{{ rangeLabel }}暂无耗时记录</div>
          </el-card>

          <el-card class="panel group-panel">
            <template #header>
              <div class="panel-head">
                <span class="group-title">
                  分组表现
                  <span class="panel-hint">不含 FAQ 直出</span>
                </span>
                <el-radio-group v-model="groupMode" size="small">
                  <el-radio-button value="models">按模型</el-radio-button>
                  <el-radio-button value="knowledge_bases">按知识库</el-radio-button>
                </el-radio-group>
              </div>
            </template>
            <div v-if="groupRows.length > 0" class="group-list">
              <div class="group-row group-head" :class="{ 'has-cost': groupMode === 'models' }">
                <span>名称</span>
                <span class="num">条数</span>
                <span class="num">失败率</span>
                <span class="num">平均</span>
                <span class="num p90-col">P90</span>
                <span v-if="groupMode === 'models'" class="num">费用</span>
              </div>
              <div
                v-for="row in groupRows"
                :key="row.key"
                class="group-row"
                :class="{ 'has-cost': groupMode === 'models' }"
              >
                <span class="group-name" :title="row.label">{{ row.label }}</span>
                <span class="num">{{ row.qa_total }}</span>
                <span class="num" :class="{ 'is-warn': row.failure_rate > 0 }">
                  {{ formatPercent(row.failure_rate) }}
                </span>
                <span class="num">{{ formatDuration(row.avg_total_duration_ms) }}</span>
                <span class="num p90-col" :style="{ '--pct': row.p90Percent + '%' }">
                  {{ formatDuration(row.p90_total_duration_ms) }}
                </span>
                <span v-if="groupMode === 'models'" class="num">
                  <template v-if="row.cost != null">¥{{ formatCost(row.cost) }}</template>
                  <span v-else class="is-muted">未定价</span>
                </span>
              </div>
            </div>
            <div v-else class="panel-empty">{{ rangeLabel }}暂无分组数据</div>
          </el-card>
        </section>
      </template>
    </template>
    <el-empty v-else-if="!loading" description="暂无统计数据" />
  </div>
</template>

<style scoped>
.dashboard {
  min-height: 100%;
  /* 右上角一层极淡的靛蓝氛围，避免整页纯灰底显得呆板。 */
  background: radial-gradient(720px 260px at 86% -60px, rgba(79, 70, 229, 0.07), transparent 70%);
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}
.head-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.range-picker {
  width: 268px;
}
.page-title {
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.2px;
  color: var(--el-text-color-primary);
}
.page-desc {
  margin-top: 4px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

/* —— 统计卡：错峰淡入 + 数字滚动 —— */
.stat-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}
.stat-card {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 18px;
  background: #fff;
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
  animation: card-rise 0.3s var(--kb-ease) backwards;
  animation-delay: var(--delay);
  transition: border-color 0.2s var(--kb-ease), box-shadow 0.2s var(--kb-ease);
}
.stat-card:hover {
  border-color: var(--el-border-color-dark);
  box-shadow: 0 6px 16px rgba(15, 23, 42, 0.07);
}
.stat-card.pending {
  cursor: pointer;
  border-color: #f4d6b4;
  transition: transform 0.15s var(--kb-ease), box-shadow 0.15s var(--kb-ease),
    border-color 0.15s var(--kb-ease);
}
.stat-card.pending:hover {
  transform: translateY(-1px);
  border-color: #e4a051;
  box-shadow: 0 6px 16px rgba(217, 119, 6, 0.12);
}
.stat-card.pending:active {
  transform: scale(0.99);
}
.stat-icon {
  width: 40px;
  height: 40px;
  flex-shrink: 0;
  display: grid;
  place-items: center;
  border-radius: 8px;
  font-size: 19px;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, #fff);
}
.stat-value {
  font-size: 30px;
  font-weight: 600;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-primary);
}
.stat-label {
  margin-top: 4px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.stat-sub {
  margin-top: 3px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
@keyframes card-rise {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
}

/* —— 面板区 —— */
.panels {
  margin-top: 16px;
  display: grid;
  grid-template-columns: 1.6fr 1fr;
  gap: 16px;
  align-items: stretch;
}
.panels .panel {
  animation: card-rise 0.32s var(--kb-ease) 0.06s backwards;
}
.panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  font-weight: 600;
}
.panel-hint {
  font-size: 12px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
}

/* —— 趋势图：柱体生长 + 成功线描边 + 悬停提示 —— */
.chart-wrap {
  position: relative;
}
.trend-chart {
  width: 100%;
  display: block;
  overflow: visible;
}
.chart-grid-label {
  font-size: 11px;
  fill: #a8adb7;
}
.chart-date-label {
  font-size: 11px;
  fill: var(--el-text-color-secondary);
}
.chart-hit {
  fill: transparent;
  cursor: crosshair;
}
.chart-bar .bar-total,
.chart-bar .bar-success {
  transform-box: fill-box;
  transform-origin: center bottom;
  animation: bar-grow 0.5s var(--kb-ease) backwards;
  animation-delay: calc(var(--i) * 16ms);
  transition: opacity 0.15s var(--kb-ease);
}
.chart-bar.is-dim .bar-total,
.chart-bar.is-dim .bar-success {
  opacity: 0.35;
}
.success-line {
  stroke: #4f46e5;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-dasharray: 1;
  stroke-dashoffset: 1;
  animation: line-draw 0.9s var(--kb-ease) 0.28s forwards;
}
.success-dot {
  fill: #fff;
  stroke: #4f46e5;
  stroke-width: 1.6;
  transform-box: fill-box;
  transform-origin: center;
  animation: dot-in 0.28s ease-out 1s backwards;
}
.chart-guide {
  stroke: #c7d2fe;
  stroke-width: 1;
  stroke-dasharray: 3 3;
}
.chart-tip {
  position: absolute;
  z-index: 2;
  min-width: 132px;
  padding: 8px 10px;
  border-radius: 6px;
  background: #1e1b4b;
  box-shadow: 0 8px 20px rgba(15, 23, 42, 0.18);
  font-size: 12px;
  line-height: 1.55;
  color: #fff;
  pointer-events: none;
  animation: tip-in 0.14s ease-out;
}
.chart-tip.is-above {
  transform: translate(-50%, calc(-100% - 10px));
}
.chart-tip.is-below {
  transform: translate(-50%, 10px);
}
.tip-date {
  margin-bottom: 2px;
  font-weight: 600;
  letter-spacing: 0.2px;
}
.tip-row {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  color: rgba(255, 255, 255, 0.72);
  font-variant-numeric: tabular-nums;
}
.tip-row b {
  font-weight: 600;
  color: #fff;
}
@keyframes bar-grow {
  from {
    transform: scaleY(0);
  }
}
@keyframes line-draw {
  to {
    stroke-dashoffset: 0;
  }
}
@keyframes dot-in {
  from {
    opacity: 0;
    transform: scale(0.4);
  }
}
@keyframes tip-in {
  from {
    opacity: 0;
  }
}

/* —— 状态分布 —— */
.qa-status-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.qa-status-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.qa-status-label {
  width: 44px;
  flex-shrink: 0;
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.qa-status-track {
  flex: 1;
  height: 8px;
  border-radius: 999px;
  background: var(--el-fill-color);
  overflow: hidden;
}
.qa-status-fill {
  height: 100%;
  border-radius: 999px;
  transition: width 0.5s var(--kb-ease);
}
.qa-status-count {
  width: 86px;
  flex-shrink: 0;
  text-align: right;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-secondary);
}

.doc-section {
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.doc-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-regular);
  margin-bottom: 10px;
}
.doc-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.doc-chip {
  padding: 3px 10px;
  border-radius: 4px;
  border: 1px solid;
  font-size: 12px;
}
.doc-chip.empty {
  color: var(--el-text-color-secondary);
  border-color: var(--el-border-color-lighter);
  background: transparent;
}

.token-section {
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.token-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}
.token-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-regular);
}
.token-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: right;
}
.token-total {
  margin-top: 10px;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.token-number {
  font-size: 26px;
  font-weight: 600;
  line-height: 1.1;
  color: var(--el-text-color-primary);
  font-variant-numeric: tabular-nums;
}
.token-unit {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.token-split-bar {
  margin-top: 10px;
  display: flex;
  height: 8px;
  border-radius: 999px;
  overflow: hidden;
  background: var(--el-fill-color);
}
.token-split-prompt {
  background: #60a5fa;
}
.token-split-completion {
  background: #4f46e5;
}
.token-chips {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.token-chip {
  padding: 3px 10px;
  border-radius: 4px;
  border: 1px solid;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.token-chip.prompt {
  color: #2563eb;
  border-color: rgba(59, 130, 246, 0.3);
  background: rgba(59, 130, 246, 0.06);
}
.token-chip.completion {
  color: #4f46e5;
  border-color: rgba(79, 70, 229, 0.3);
  background: rgba(79, 70, 229, 0.06);
}
.token-spark {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 48px;
  margin-top: 12px;
}
.token-bar {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  height: var(--h);
  min-height: 3px;
  border-radius: 2px 2px 0 0;
  overflow: hidden;
  background: var(--el-fill-color);
  transform-origin: center bottom;
  animation: bar-grow 0.45s var(--kb-ease) backwards;
  animation-delay: calc(var(--i) * 16ms);
}
.token-bar i {
  display: block;
  width: 100%;
}
.token-bar .seg-completion {
  background: #4f46e5;
}
.token-bar .seg-prompt {
  flex: 1;
  background: #a5b4fc;
}
.token-bar:hover .seg-completion {
  background: #4338ca;
}
.token-empty {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
/* —— 成本核算：与 token 用量同一张卡，金额用等宽数字避免跳动 —— */
.cost-line {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--el-border-color-lighter);
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-variant-numeric: tabular-nums;
}
.cost-main {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
}
.cost-main em {
  font-style: normal;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.cost-main b {
  font-size: 16px;
  font-weight: 600;
  color: #4f46e5;
}
.cost-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.cost-link {
  margin-left: auto;
}
.cost-warn {
  margin-top: 6px;
  font-size: 12px;
  color: #b45309;
}

/* —— 近 14 天质量与耗时 —— */
.quality-strip {
  margin-top: 16px;
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}
.quality-tile {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 14px 16px 14px 18px;
  background: #fff;
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
  overflow: hidden;
  animation: card-rise 0.3s var(--kb-ease) backwards;
  animation-delay: var(--delay);
}
.quality-tile::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: var(--accent);
  opacity: 0.75;
}
.quality-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.quality-value {
  font-size: 22px;
  font-weight: 600;
  line-height: 1.25;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-primary);
}
.quality-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.insights {
  grid-template-columns: 1fr 1.25fr;
}
.panel-empty {
  padding: 10px 0 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.stage-list {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.stage-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}
.stage-name {
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.stage-nums {
  display: flex;
  gap: 10px;
}
.stage-num {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-secondary);
}
.stage-num em {
  font-style: normal;
  font-size: 10px;
  letter-spacing: 0.3px;
  color: #a8adb7;
}
.stage-num.p90 {
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.stage-track {
  position: relative;
  margin-top: 7px;
  height: 8px;
  border-radius: 999px;
  background: var(--el-fill-color);
}
.stage-fill {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  border-radius: 999px;
  background: linear-gradient(90deg, #a5b4fc, #6366f1);
  transition: width 0.5s var(--kb-ease);
}
.stage-mark {
  position: absolute;
  top: -3px;
  width: 2px;
  height: 14px;
  border-radius: 2px;
  background: #1e1b4b;
  opacity: 0.4;
  transform: translateX(-1px);
}

.group-panel .panel-head {
  align-items: center;
}
.group-title {
  display: inline-flex;
  align-items: baseline;
  gap: 8px;
}
.group-list {
  max-height: 248px;
  overflow-y: auto;
}
.group-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 44px 58px 58px 92px;
  gap: 8px;
  align-items: center;
  padding: 7px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
  font-size: 12px;
  color: var(--el-text-color-regular);
}
/* 按模型分组时多一列「费用」，其余列略收窄保持单行。 */
.group-row.has-cost {
  grid-template-columns: minmax(0, 1fr) 40px 54px 54px 84px 64px;
}
.group-row .is-muted {
  color: var(--el-text-color-placeholder);
}
.group-row:last-child {
  border-bottom: none;
}
.group-head {
  padding-top: 0;
  font-size: 11px;
  color: var(--el-text-color-secondary);
  border-bottom-color: var(--el-border-color);
}
.group-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--el-text-color-primary);
}
.group-row .num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.group-row .is-warn {
  color: #dc2626;
}
.p90-col {
  padding: 2px 8px;
  border-radius: 4px;
  background: linear-gradient(
    90deg,
    rgba(79, 70, 229, 0.14) var(--pct, 0%),
    transparent var(--pct, 0%)
  );
}

@media (max-width: 1100px) {
  .stat-cards {
    grid-template-columns: repeat(2, 1fr);
  }
  .quality-strip {
    grid-template-columns: repeat(2, 1fr);
  }
  .panels {
    grid-template-columns: 1fr;
  }
  .insights {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 760px) {
  .page-head {
    flex-wrap: wrap;
  }
  .head-controls {
    width: 100%;
  }
  .range-picker {
    flex: 1;
    width: auto;
  }
  .stat-cards,
  .quality-strip {
    grid-template-columns: 1fr;
  }
}
</style>
