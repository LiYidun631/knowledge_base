<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search } from '@element-plus/icons-vue'
import { client, errorText } from '../api/client'
import PageHeader from '../components/ui/PageHeader.vue'
import DataPanel from '../components/ui/DataPanel.vue'
import StatusPill from '../components/ui/StatusPill.vue'
import EmptyState from '../components/ui/EmptyState.vue'

interface GlobalSearchHit {
  kind: GroupKind
  id: string
  title: string
  snippet: string | null
  subtitle: string | null
  status: string | null
  knowledge_base_id: string | null
  created_at: string | null
}

interface GlobalSearchGroup {
  kind: GroupKind
  total: number
  items: GlobalSearchHit[]
}

type GroupKind =
  | 'knowledge_base'
  | 'document'
  | 'faq'
  | 'qa_record'
  | 'project_trace'
  | 'product'

interface Part {
  text: string
  hit: boolean
}

interface RenderHit extends GlobalSearchHit {
  titleParts: Part[]
  snippetParts: Part[]
  statusLabel: string
  statusTone: PillTone | null
}

interface RenderGroup {
  kind: GroupKind
  label: string
  total: number
  hits: RenderHit[]
}

const KIND_LABELS: Record<GroupKind, string> = {
  knowledge_base: '知识库',
  document: '文档',
  faq: 'FAQ',
  qa_record: '问答记录',
  project_trace: '项目轨迹',
  product: '产品目录',
}

// 各资源的状态码含义不同，统一在此翻译成一句短标签。
const STATUS_LABELS: Record<GroupKind, Record<string, string>> = {
  knowledge_base: { active: '启用中', archived: '已归档' },
  document: {
    queued: '排队中',
    processing: '入库中',
    completed: '已完成',
    failed: '失败',
    deleting: '删除中',
  },
  faq: { auto: '自动生成', manual: '手动添加' },
  qa_record: { success: '成功', no_results: '未命中', failed: '失败', running: '处理中' },
  project_trace: {
    rag: '检索回答',
    direct: '直出',
    clarification: '澄清追问',
    chat: '闲聊',
    health_data: '健康数据',
    report: '报告解读',
    refusal: '拒答',
  },
  product: {},
}

type PillTone = 'success' | 'warning' | 'danger' | 'info' | 'primary'

// 状态码按语义着色，和列表页的状态胶囊保持同一套语气。
const STATUS_TONES: Record<GroupKind, Record<string, PillTone>> = {
  knowledge_base: { active: 'success', archived: 'info' },
  document: {
    queued: 'info',
    processing: 'warning',
    completed: 'success',
    failed: 'danger',
    deleting: 'info',
  },
  faq: { auto: 'info', manual: 'primary' },
  qa_record: { success: 'success', no_results: 'warning', failed: 'danger', running: 'primary' },
  project_trace: {
    rag: 'primary',
    direct: 'primary',
    clarification: 'warning',
    chat: 'info',
    health_data: 'info',
    report: 'info',
    refusal: 'danger',
  },
  product: {},
}

const route = useRoute()
const router = useRouter()
const input = ref('')
const keyword = ref('')
const loading = ref(false)
const error = ref('')
const tookMs = ref(0)
const groups = ref<RenderGroup[]>([])

const totalHits = computed(() => groups.value.reduce((sum, group) => sum + group.total, 0))

function splitByKeyword(text: string | null, needle: string): Part[] {
  if (!text) return []
  if (!needle) return [{ text, hit: false }]
  const lowered = text.toLowerCase()
  const parts: Part[] = []
  let cursor = 0
  let index = lowered.indexOf(needle, cursor)
  while (index >= 0) {
    if (index > cursor) parts.push({ text: text.slice(cursor, index), hit: false })
    parts.push({ text: text.slice(index, index + needle.length), hit: true })
    cursor = index + needle.length
    index = lowered.indexOf(needle, cursor)
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor), hit: false })
  return parts
}

function formatTime(value: string | null): string {
  return value ? value.slice(0, 16).replace('T', ' ') : ''
}

function renderGroups(groupsData: GlobalSearchGroup[]): RenderGroup[] {
  return groupsData
    .filter((group) => group.total > 0)
    .map((group) => ({
      kind: group.kind,
      label: KIND_LABELS[group.kind],
      total: group.total,
      hits: group.items.map((hit) => ({
        ...hit,
        titleParts: splitByKeyword(hit.title, keyword.value),
        snippetParts: splitByKeyword(hit.snippet, keyword.value),
        statusLabel: hit.status ? STATUS_LABELS[hit.kind][hit.status] ?? hit.status : '',
        statusTone: hit.status ? STATUS_TONES[hit.kind][hit.status] ?? 'info' : null,
      })),
    }))
}

async function runSearch() {
  if (!keyword.value) {
    groups.value = []
    tookMs.value = 0
    error.value = ''
    return
  }
  loading.value = true
  error.value = ''
  try {
    const { data } = await client.get<{
      q: string
      took_ms: number
      groups: GlobalSearchGroup[]
    }>('/admin/search', { params: { q: keyword.value, limit: 5 } })
    tookMs.value = data.took_ms
    groups.value = renderGroups(data.groups)
  } catch (caught) {
    groups.value = []
    error.value = errorText(caught, '搜索失败，请稍后重试')
  } finally {
    loading.value = false
  }
}

// 检索词放回地址栏，便于刷新、分享和后退。
async function submit() {
  const value = input.value.trim()
  if (value === keyword.value) return
  if (!value) {
    await router.replace({ path: '/search' })
    return
  }
  await router.replace({ path: '/search', query: { q: value } })
}

watch(
  () => route.query.q,
  (value) => {
    const next = typeof value === 'string' ? value.trim() : ''
    input.value = next
    keyword.value = next
    void runSearch()
  },
  { immediate: true },
)

onMounted(() => {
  if (!keyword.value) {
    // 进入空页面时把焦点放在输入框，少一次点击。
    document.querySelector<HTMLInputElement>('.search-input')?.focus()
  }
})

function goHit(hit: GlobalSearchHit) {
  const term = keyword.value
  switch (hit.kind) {
    case 'knowledge_base':
      void router.push(`/kbs/${hit.id}`)
      break
    case 'document':
      void router.push({ path: `/kbs/${hit.knowledge_base_id}`, query: { doc: hit.id } })
      break
    case 'faq':
      void router.push({ path: '/faqs', query: { kb: hit.knowledge_base_id, q: term } })
      break
    case 'qa_record':
      void router.push({ path: '/qa-records', query: { q: term } })
      break
    case 'project_trace':
      void router.push({ path: '/project-traces', query: { q: term } })
      break
    case 'product':
      void router.push({ path: '/catalog', query: { q: term } })
      break
  }
}

// 文档与 FAQ 没有跨库列表页，只在卡片头部标总数，不提供「查看全部」。
const SEE_ALL: Partial<Record<GroupKind, string>> = {
  knowledge_base: '/kbs',
  qa_record: '/qa-records',
  project_trace: '/project-traces',
  product: '/catalog',
}

function seeAllTarget(group: RenderGroup): string | null {
  return SEE_ALL[group.kind] ?? null
}

function goSeeAll(group: RenderGroup) {
  const path = seeAllTarget(group)
  if (!path) return
  void router.push({ path, query: { q: keyword.value } })
}
</script>

<template>
  <section class="search-page">
    <PageHeader
      title="全局搜索"
      eyebrow="一键六类"
      subtitle="知识库、文档、FAQ、问答记录、项目轨迹、产品目录 · 结果按类型分组，命中词高亮"
    >
      <template #meta>
        <span v-if="keyword && !error" class="header-count">
          「{{ keyword }}」命中 {{ totalHits }} 条 · {{ tookMs }} ms
        </span>
      </template>
    </PageHeader>

    <form class="search-bar" @submit.prevent="submit">
      <el-icon class="search-bar-icon"><Search /></el-icon>
      <input
        v-model="input"
        class="search-input"
        type="search"
        maxlength="100"
        placeholder="搜索知识库、文档、FAQ、问答记录、项目轨迹、产品目录"
        aria-label="全局搜索关键词"
      />
      <button class="search-submit" type="submit" :disabled="!input.trim()">搜索</button>
    </form>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
      class="error-alert"
    />

    <DataPanel v-if="!keyword">
      <EmptyState
        title="从一个关键词开始"
        text="例如产品名、文档文件名、FAQ 问题、用户提问或项目标识；只搜索字面文本，不做同义词扩展。"
      />
    </DataPanel>

    <div v-else-if="loading" class="result-grid" aria-busy="true">
      <div v-for="index in 3" :key="index" class="result-card is-skeleton">
        <span class="skeleton-line is-head" />
        <span class="skeleton-line" />
        <span class="skeleton-line is-short" />
      </div>
    </div>

    <DataPanel v-else-if="groups.length === 0 && !error">
      <EmptyState
        :title="`没有匹配「${keyword}」的内容`"
        text="试试更短的关键词，或去掉型号、编号等限定词。"
        muted
      />
    </DataPanel>

    <div v-else class="result-grid">
      <article
        v-for="(group, index) in groups"
        :key="group.kind"
        class="result-card"
        :style="{ animationDelay: `${index * 40}ms` }"
      >
        <header class="card-head">
          <span class="kind-chip" :class="`kind-${group.kind}`">{{ group.label }}</span>
          <span class="card-count">共 {{ group.total }} 条</span>
          <button
            v-if="seeAllTarget(group)"
            class="card-more"
            type="button"
            @click="goSeeAll(group)"
          >
            查看全部 →
          </button>
        </header>

        <ul class="hit-list">
          <li v-for="hit in group.hits" :key="hit.id" class="hit" @click="goHit(hit)">
            <p class="hit-title">
              <template v-for="(part, partIndex) in hit.titleParts" :key="partIndex">
                <mark v-if="part.hit">{{ part.text }}</mark>
                <template v-else>{{ part.text }}</template>
              </template>
            </p>
            <p v-if="hit.snippet" class="hit-snippet">
              <template v-for="(part, partIndex) in hit.snippetParts" :key="partIndex">
                <mark v-if="part.hit">{{ part.text }}</mark>
                <template v-else>{{ part.text }}</template>
              </template>
            </p>
            <div class="hit-meta">
              <span v-if="hit.subtitle" class="hit-owner">{{ hit.subtitle }}</span>
              <StatusPill
                v-if="hit.statusLabel"
                :label="hit.statusLabel"
                :tone="hit.statusTone ?? 'info'"
                :dot="false"
              />
              <span v-if="hit.created_at" class="hit-time">{{ formatTime(hit.created_at) }}</span>
            </div>
          </li>
        </ul>
      </article>
    </div>
  </section>
</template>

<style scoped>
.search-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
/* 间距统一由 flex gap 控制，页头自带的 margin 会变成双倍。 */
.search-page > .page-header {
  margin-bottom: 0;
}
.header-count {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}

/* 检索框：单行、等宽留白，聚焦时只用描边和阴影表态，不做弹跳。 */
.search-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  background: #fff;
  border: 1px solid var(--el-border-color);
  border-radius: var(--kb-panel-radius);
  padding: 6px 6px 6px 14px;
  transition:
    border-color 0.18s var(--kb-ease),
    box-shadow 0.18s var(--kb-ease);
}

.search-bar:focus-within {
  border-color: var(--el-color-primary-light-5);
  box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.08);
}

.search-bar-icon {
  color: var(--el-text-color-placeholder);
  font-size: 16px;
}

.search-input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  font-size: var(--kb-font-md);
  color: var(--el-text-color-primary);
  padding: 6px 0;
}

.search-input::placeholder {
  color: var(--el-text-color-placeholder);
}

.search-submit {
  border: none;
  border-radius: 6px;
  background: var(--el-color-primary);
  color: #fff;
  font-size: var(--kb-font-sm);
  padding: 7px 16px;
  cursor: pointer;
  transition:
    background-color 0.18s ease,
    transform 0.12s ease;
}

.search-submit:hover:not(:disabled) {
  background: var(--el-color-primary-dark-2);
}

.search-submit:active:not(:disabled) {
  transform: scale(0.97);
}

.search-submit:disabled {
  background: #c9cbf0;
  cursor: not-allowed;
}

.error-alert {
  border-radius: var(--kb-panel-radius);
}

/* 结果：宽屏两列，卡片等高线对齐顶部；窄屏自然堆叠。 */
.result-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  gap: 14px;
  align-items: start;
}

.result-card {
  background: var(--kb-panel-bg);
  border: 1px solid var(--kb-panel-border);
  border-radius: var(--kb-panel-radius);
  box-shadow: var(--kb-panel-shadow);
  padding: 12px 14px 8px;
  animation: card-rise 0.28s var(--kb-ease) both;
}

@keyframes card-rise {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.kind-chip {
  font-size: var(--kb-font-2xs);
  line-height: 1;
  padding: 4px 8px;
  border-radius: 999px;
  letter-spacing: 0.02em;
}

.kind-knowledge_base {
  background: #eef0fd;
  color: #4f46e5;
}

.kind-document {
  background: #e8f4f1;
  color: #0f766e;
}

.kind-faq {
  background: #fbf3e6;
  color: #b45309;
}

.kind-qa_record {
  background: #f1edfb;
  color: #6d28d9;
}

.kind-project_trace {
  background: #e8f6ed;
  color: #15803d;
}

.kind-product {
  background: #eef1f5;
  color: #475569;
}

.card-count {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
}

.card-more {
  margin-left: auto;
  border: none;
  background: transparent;
  color: var(--el-color-primary);
  font-size: var(--kb-font-xs);
  cursor: pointer;
  padding: 2px 4px;
  border-radius: 6px;
  transition: background-color 0.15s ease;
}

.card-more:hover {
  background: var(--el-color-primary-light-9);
}

.hit-list {
  list-style: none;
  margin: 2px 0 0;
  padding: 0;
}

.hit {
  position: relative;
  padding: 9px 8px 9px 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: background-color 0.15s ease;
}

/* hover 时左侧浮出 2px 竖条，替代整块变色，保持克制。 */
.hit::before {
  content: '';
  position: absolute;
  left: 0;
  top: 10px;
  bottom: 10px;
  width: 2px;
  border-radius: 2px;
  background: transparent;
  transition: background-color 0.15s ease;
}

.hit:hover {
  background: var(--el-fill-color-light);
}

.hit:hover::before {
  background: var(--el-color-primary);
}

.hit + .hit {
  border-top: 1px solid var(--el-border-color-lighter);
}

.hit-title {
  margin: 0;
  font-size: var(--kb-font-sm);
  line-height: 1.5;
  color: var(--el-text-color-primary);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.hit-snippet {
  margin: 3px 0 0;
  font-size: var(--kb-font-xs);
  line-height: 1.55;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

mark {
  background: rgba(79, 70, 229, 0.12);
  color: #3730a3;
  border-radius: 3px;
  padding: 0 2px;
}

.hit-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 5px;
  font-size: var(--kb-font-2xs);
  color: var(--el-text-color-placeholder);
}

.hit-time {
  font-variant-numeric: tabular-nums;
}

.is-skeleton {
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 16px 14px;
}

.skeleton-line {
  height: 10px;
  border-radius: 4px;
  background: linear-gradient(
    90deg,
    var(--el-fill-color-light) 25%,
    var(--el-fill-color) 37%,
    var(--el-fill-color-light) 63%
  );
  background-size: 400% 100%;
  animation: shimmer 1.2s ease-in-out infinite;
}

.skeleton-line.is-head {
  width: 42%;
  height: 14px;
}

.skeleton-line.is-short {
  width: 65%;
}

@keyframes shimmer {
  from {
    background-position: 100% 50%;
  }
  to {
    background-position: 0 50%;
  }
}

@media (max-width: 760px) {
  .result-grid {
    grid-template-columns: 1fr;
  }

  .search-input {
    font-size: 13px;
  }
}
</style>
