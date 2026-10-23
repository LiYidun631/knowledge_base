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

interface Statistics {
  companies_total: number
  knowledge_bases_total: number
  documents_total: number
  documents_by_status: Record<string, number>
  qa_total: number
  qa_by_status: Record<string, number>
  review_pending: number
  token_usage: TokenUsage
  daily_trend: DailyTrendPoint[]
}

const router = useRouter()
const loading = ref(false)
const stats = ref<Statistics | null>(null)
const animated = reactive({ kbs: 0, docs: 0, qa: 0, pending: 0 })

async function load() {
  loading.value = true
  try {
    const { data } = await client.get<Statistics>('/admin/statistics')
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

const chart = computed(() => {
  const trend = stats.value?.daily_trend ?? []
  const width = 640
  const height = 240
  const plotTop = 24
  const plotBottom = 208
  const plotLeft = 44
  const plotRight = 624
  const plotHeight = plotBottom - plotTop
  const slot = trend.length > 0 ? (plotRight - plotLeft) / trend.length : 0
  const barWidth = slot * 0.58
  const max = Math.max(1, ...trend.map((point) => point.qa_total))
  return { trend, max, width, height, plotTop, plotBottom, plotLeft, plotRight, plotHeight, slot, barWidth }
})

function barX(index: number): number {
  return chart.value.plotLeft + index * chart.value.slot + (chart.value.slot - chart.value.barWidth) / 2
}

function barHeight(value: number): number {
  return (Math.min(value, chart.value.max) / chart.value.max) * chart.value.plotHeight
}

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
          <div class="page-desc">关键指标与近 14 天问答表现</div>
        </div>
        <el-button :icon="Refresh" circle aria-label="刷新统计数据" @click="load" />
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
              <span>近 14 天问答趋势</span>
              <span class="panel-hint">深色为成功问答</span>
            </div>
          </template>
          <svg
            class="trend-chart"
            :viewBox="`0 0 ${chart.width} ${chart.height}`"
            role="img"
            aria-label="近 14 天问答趋势柱状图"
          >
            <g v-for="fraction in [0, 0.25, 0.5, 0.75, 1]" :key="fraction">
              <line
                :x1="chart.plotLeft"
                :x2="chart.plotRight"
                :y1="chart.plotBottom - fraction * chart.plotHeight"
                :y2="chart.plotBottom - fraction * chart.plotHeight"
                stroke="#eef0f3"
                stroke-width="1"
              />
              <text
                :x="chart.plotLeft - 8"
                :y="chart.plotBottom - fraction * chart.plotHeight + 4"
                text-anchor="end"
                class="chart-grid-label"
              >
                {{ Math.round(chart.max * fraction) }}
              </text>
            </g>
            <g
              v-for="(point, index) in chart.trend"
              :key="point.date"
              class="chart-bar"
            >
              <title>
                {{ point.date }}：共 {{ point.qa_total }} 次（成功 {{ point.qa_success }}）·
                平均耗时 {{ formatDuration(point.avg_total_duration_ms) }} ·
                {{ formatTokens(point.total_tokens) }} tokens
              </title>
              <rect
                :x="barX(index)"
                :y="chart.plotBottom - barHeight(point.qa_total)"
                :width="chart.barWidth"
                :height="barHeight(point.qa_total)"
                rx="4"
                fill="#e0e7ff"
              />
              <rect
                v-if="point.qa_success > 0"
                :x="barX(index)"
                :y="chart.plotBottom - barHeight(point.qa_success)"
                :width="chart.barWidth"
                :height="barHeight(point.qa_success)"
                rx="4"
                fill="#4f46e5"
              />
              <text
                v-if="index % 2 === 0 || index === chart.trend.length - 1"
                :x="barX(index) + chart.barWidth / 2"
                :y="chart.plotBottom + 18"
                text-anchor="middle"
                class="chart-date-label"
              >
                {{ point.date.slice(5) }}
              </text>
            </g>
          </svg>
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
                {{ stats.token_usage.measured_qa_count }} 条已记录 · 近 14 天
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
            </template>
            <div v-else class="token-empty">暂无用量数据；FAQ 直出与历史记录不计入。</div>
          </div>
        </el-card>
      </section>
    </template>
    <el-empty v-else-if="!loading" description="暂无统计数据" />
  </div>
</template>

<style scoped>
.dashboard {
  min-height: 100%;
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 20px;
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

.trend-chart {
  width: 100%;
  display: block;
}
.chart-grid-label {
  font-size: 11px;
  fill: #a8adb7;
}
.chart-date-label {
  font-size: 11px;
  fill: var(--el-text-color-secondary);
}
.chart-bar {
  cursor: default;
}
.chart-bar rect {
  transition: opacity 0.15s ease;
}
.chart-bar:hover rect {
  opacity: 0.75;
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
.token-empty {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

@media (max-width: 1100px) {
  .stat-cards {
    grid-template-columns: repeat(2, 1fr);
  }
  .panels {
    grid-template-columns: 1fr;
  }
}
</style>
