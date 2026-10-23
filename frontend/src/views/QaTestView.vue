<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { client, errorMessage, getToken, handleAuthStatus } from '../api/client'
import { renderMarkdown } from '../utils/markdown'
import { resolveInlineImages } from '../utils/assets'

interface Company {
  id: string
  code: string
  name: string
  status: string
}

interface KnowledgeBase {
  id: string
  company_id: string | null
  name: string
  status: string
}

interface SourceItem {
  filename: string
  page_number: number | null
  section: string | null
}

interface AnswerSource {
  rank: number
  content: string
  source: SourceItem
  scores: {
    cosine_similarity: number | null
    bm25_score: number | null
    rrf_score: number | null
    rerank_score: number | null
  }
}

interface AnswerResponse {
  answer: string
  clarification_required: boolean
  clarification_question: string | null
  clarification_options: string[]
  retrieval_mode: string
  sources: AnswerSource[]
}

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  sources: AnswerSource[]
  clarification: { question: string; options: string[] } | null
  streaming?: boolean
}

interface ProductOptions {
  product_names: string[]
  model_names: string[]
}

interface FaqCard {
  id: string
  question: string
}

interface SuggestRow {
  width: number
  items: { card: FaqCard; index: number }[]
}

const companies = ref<Company[]>([])
const kbs = ref<KnowledgeBase[]>([])
const selectedCompanyId = ref('')
const selectedKbId = ref('')
const productOptions = ref<ProductOptions>({ product_names: [], model_names: [] })
const productName = ref('')
const modelName = ref('')
const mode = ref<'hybrid' | 'bm25' | 'vector'>('hybrid')
const topK = ref(5)
const modeOptions = [
  { value: 'hybrid', label: '混合检索' },
  { value: 'bm25', label: 'BM25' },
  { value: 'vector', label: '向量' },
]
const topKOptions = [1, 3, 5, 10, 20, 50]
const includeContext = ref(true)
const question = ref('')
const asking = ref(false)
const messages = ref<ChatMessage[]>([])
const messagesRef = ref<HTMLDivElement | null>(null)
const sessionId = ref('')
const streamAbort = ref<AbortController | null>(null)
const sourceDialogVisible = ref(false)
const activeSource = ref<AnswerSource | null>(null)
// 推荐问题卡片：库级恒显，文档级随产品/型号过滤即时换池。
const faqCards = ref<FaqCard[]>([])

const activeCompanies = computed(() => companies.value.filter((c) => c.status === 'active'))
const selectedKb = computed(() => kbs.value.find((k) => k.id === selectedKbId.value) ?? null)
const modeLabel = computed(() => modeOptions.find((m) => m.value === mode.value)?.label ?? mode.value)

function pad2(value: number): string {
  return String(value).padStart(2, '0')
}

// 推荐问题按「上宽下窄」的漏斗排布：1 条单行；2 条各占一行；更多时每行两条逐行收窄。
const suggestRows = computed<SuggestRow[]>(() => {
  const cards = faqCards.value
  if (cards.length === 0) return []
  if (cards.length === 1) {
    return [{ width: 76, items: [{ card: cards[0], index: 0 }] }]
  }
  if (cards.length === 2) {
    return cards.map((card, index) => ({
      width: index === 0 ? 92 : 70,
      items: [{ card, index }],
    }))
  }
  const rows: SuggestRow[] = []
  cards.forEach((card, index) => {
    const rowIndex = Math.floor(index / 2)
    rows[rowIndex] ??= { width: 0, items: [] }
    rows[rowIndex].items.push({ card, index })
  })
  const last = rows.length - 1
  rows.forEach((row, rowIndex) => {
    row.width = last === 0 ? 76 : Math.round(100 - (rowIndex * 26) / last)
  })
  return rows
})

function setMode(value: unknown) {
  mode.value = value as 'hybrid' | 'bm25' | 'vector'
}

function setTopK(value: unknown) {
  topK.value = Number(value)
}

function scrollToBottom(smooth: boolean) {
  void nextTick(() => {
    const container = messagesRef.value
    if (!container) return
    container.scrollTo({ top: container.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  })
}

watch(
  () => messages.value.length,
  () => scrollToBottom(true),
)

function openSource(source: AnswerSource) {
  activeSource.value = source
  sourceDialogVisible.value = true
}

// 点击答案里的图片（blob URL）时在新标签打开原图，方便放大查看。
function onMessageClick(event: MouseEvent) {
  const target = event.target
  if (target instanceof HTMLImageElement && target.src.startsWith('blob:')) {
    window.open(target.src, '_blank')
  }
}

// 来源预览保持纯文本截断展示，去掉图片语法避免显示 ![](url) 原文。
function previewText(content: string): string {
  return content.replace(/!\[[^\]]*\]\([^)]+\)/g, '').trim()
}

async function loadCompanies() {
  const { data } = await client.get<Company[]>('/companies')
  companies.value = data
}

async function loadKbs() {
  if (!selectedCompanyId.value) {
    kbs.value = []
    return
  }
  const { data } = await client.get<KnowledgeBase[]>('/knowledge-bases', {
    params: { company_id: selectedCompanyId.value, limit: 200 },
  })
  kbs.value = data.filter((kb) => kb.status === 'active')
  if (!kbs.value.some((kb) => kb.id === selectedKbId.value)) {
    selectedKbId.value = kbs.value[0]?.id ?? ''
  }
}

async function loadProductOptions() {
  if (!selectedKbId.value) {
    productOptions.value = { product_names: [], model_names: [] }
    return
  }
  const { data } = await client.get<ProductOptions>(
    `/knowledge-bases/${selectedKbId.value}/product-options`,
  )
  productOptions.value = data
}

async function loadFaqCards() {
  if (!selectedKbId.value) {
    faqCards.value = []
    return
  }
  try {
    const { data } = await client.get<FaqCard[]>(
      `/admin/knowledge-bases/${selectedKbId.value}/faqs/random`,
      {
        params: {
          limit: 6,
          // 清空 el-select 时 EP 会回传 undefined，用 ?. 避免 trim 抛错导致卡片清空。
          product_name: productName.value?.trim() || null,
          model_name: modelName.value?.trim() || null,
        },
      },
    )
    faqCards.value = data
  } catch {
    faqCards.value = []
  }
}

// 切换知识库时清空产品过滤并重载该库的产品选项。
watch(selectedKbId, () => {
  productName.value = ''
  modelName.value = ''
  void loadProductOptions().catch(() => undefined)
})

// 知识库或产品/型号变化时立即换一批推荐问题。
watch([selectedKbId, productName, modelName], () => {
  void loadFaqCards()
})

function startNewSession() {
  streamAbort.value?.abort()
  messages.value = []
  sessionId.value = crypto.randomUUID()
}

function scoreSummary(source: AnswerSource): string {
  const { scores } = source
  const parts: string[] = []
  if (scores.rrf_score !== null && scores.rrf_score !== undefined) {
    parts.push(`RRF ${scores.rrf_score.toFixed(4)}`)
  }
  if (scores.rerank_score !== null && scores.rerank_score !== undefined) {
    parts.push(`Rerank ${scores.rerank_score.toFixed(4)}`)
  }
  if (scores.bm25_score !== null && scores.bm25_score !== undefined) {
    parts.push(`BM25 ${scores.bm25_score.toFixed(2)}`)
  }
  if (scores.cosine_similarity !== null && scores.cosine_similarity !== undefined) {
    parts.push(`相似度 ${scores.cosine_similarity.toFixed(3)}`)
  }
  return parts.join(' · ')
}

// 解析一条 SSE 帧（event: 名 + data: JSON），解析失败返回 null。
function parseSseFrame<T>(frame: string): [string, T] | null {
  let name = ''
  let data = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) name = line.slice(6).trim()
    else if (line.startsWith('data:')) data += line.slice(5).trim()
  }
  if (!name || !data) return null
  try {
    return [name, JSON.parse(data) as T]
  } catch {
    return null
  }
}

// 流式问答：axios 不解析 SSE，改用 fetch 逐帧读取 meta/delta/done/error。
// delta 直接追加进气泡；done 的最终答案已还原图片、表格已转列表，整体替换流式文本。
async function streamAsk(kbId: string, companyId: string, text: string, messageIndex: number) {
  const controller = new AbortController()
  streamAbort.value = controller
  const token = getToken() ?? ''
  let response: Response
  try {
    response = await fetch(`/api/v1/knowledge-bases/${kbId}/ask/stream?company_id=${companyId}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        query: text,
        top_k: topK.value,
        retrieval_mode: mode.value,
        include_context: includeContext.value,
        session_id: sessionId.value || null,
        // 产品过滤可选：选定后只在对应产品/型号的文档内检索。
        product_name: productName.value?.trim() || null,
        model_name: modelName.value?.trim() || null,
      }),
      signal: controller.signal,
    })
  } catch (error) {
    if (!controller.signal.aborted) errorMessage(error, '问答失败')
    return
  }
  if (response.status === 401 || response.status === 403) {
    handleAuthStatus(response.status)
    return
  }
  if (!response.ok || !response.body) {
    let detail = ''
    try {
      const payload = (await response.json()) as { detail?: unknown }
      if (typeof payload.detail === 'string') detail = payload.detail
    } catch {
      detail = ''
    }
    ElMessage.error(detail || '问答失败')
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let donePayload: AnswerResponse | null = null
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let splitAt: number
      while ((splitAt = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, splitAt)
        buffer = buffer.slice(splitAt + 2)
        const parsed = parseSseFrame<{ text?: string } | AnswerResponse | { detail?: string }>(frame)
        if (!parsed) continue
        const [name, data] = parsed
        if (name === 'delta' && 'text' in data && typeof data.text === 'string') {
          if (controller.signal.aborted) return
          // 流式期间隐藏完整出现的图片占位符；跨增量拆散的由 done 帧整体替换兜底。
          messages.value[messageIndex].content += data.text.replace(
            /\[\[KB_ASSET_KEY:[^\]]*\]\]/g,
            '（图片加载中）',
          )
          scrollToBottom(false)
        } else if (name === 'done') {
          donePayload = data as AnswerResponse
        } else if (name === 'error' && 'detail' in data) {
          ElMessage.error(typeof data.detail === 'string' ? data.detail : '问答失败')
        }
      }
    }
  } catch (error) {
    if (!controller.signal.aborted) errorMessage(error, '问答失败')
    return
  }
  if (controller.signal.aborted || !donePayload) return
  const [resolvedAnswer, resolvedSources] = await Promise.all([
    resolveInlineImages(donePayload.answer),
    Promise.all(
      donePayload.sources.map(async (source) => ({
        ...source,
        content: await resolveInlineImages(source.content),
      })),
    ),
  ])
  const message = messages.value[messageIndex]
  if (!message) return
  message.content = resolvedAnswer
  message.sources = resolvedSources
  message.clarification =
    donePayload.clarification_required && donePayload.clarification_question
      ? {
          question: donePayload.clarification_question,
          options: donePayload.clarification_options,
        }
      : null
  scrollToBottom(true)
}

async function ask(forcedQuestion?: string) {
  const text = (forcedQuestion ?? question.value).trim()
  if (!text) return
  const kb = selectedKb.value
  if (!kb || !kb.company_id) {
    ElMessage.warning('请先选择知识库')
    return
  }
  question.value = ''
  messages.value.push({ role: 'user', content: text, sources: [], clarification: null })
  const messageIndex = messages.value.length
  messages.value.push({
    role: 'assistant',
    content: '',
    sources: [],
    clarification: null,
    streaming: true,
  })
  asking.value = true
  try {
    await streamAsk(kb.id, kb.company_id, text, messageIndex)
  } finally {
    asking.value = false
    streamAbort.value = null
    const message = messages.value[messageIndex]
    if (message) message.streaming = false
  }
}

onMounted(async () => {
  try {
    await loadCompanies()
    selectedCompanyId.value = activeCompanies.value[0]?.id ?? ''
    await loadKbs()
    startNewSession()
  } catch (error) {
    errorMessage(error, '加载基础数据失败')
  }
})
</script>

<template>
  <div class="qa-page">
    <!-- 开场区：对话开始后整体淡出并收起，把高度让给消息区 -->
    <transition name="intro">
      <div v-if="messages.length === 0" class="qa-intro">
        <div class="qa-intro-inner">
          <header class="qa-hero">
            <h1 class="hero-title">HI，我是YONKER知识助手，让您的知识触手可及</h1>
            <p class="hero-sub">
              {{ selectedKb ? `当前知识库：${selectedKb.name}` : '选择知识库后开始测试问答' }}
            </p>
          </header>

          <section
            v-if="faqCards.length > 0"
            class="faq-funnel"
            :class="{
              'is-one': faqCards.length === 1,
              'is-two': faqCards.length === 2,
            }"
            aria-label="推荐问题"
          >
            <span class="funnel-aura" aria-hidden="true"></span>
            <svg class="funnel-outline" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
              <path
                d="M 1 0.8 L 99 0.8 L 84.5 99.2 L 15.5 99.2 Z"
                vector-effect="non-scaling-stroke"
              />
            </svg>
            <div class="funnel-head">
              <span class="funnel-dot" aria-hidden="true"></span>
              <span class="funnel-eyebrow">你可以这样问我</span>
              <span class="funnel-count">{{ pad2(faqCards.length) }}</span>
            </div>
            <div class="funnel-rows">
              <div
                v-for="(row, rowIndex) in suggestRows"
                :key="rowIndex"
                class="funnel-row"
                :class="{ 'is-single': row.items.length === 1 }"
                :style="{ '--row-w': `${row.width}%`, '--r': rowIndex }"
              >
                <button
                  v-for="item in row.items"
                  :key="item.card.id"
                  type="button"
                  class="funnel-item"
                  :style="{ '--i': item.index }"
                  @click="ask(item.card.question)"
                >
                  <span class="funnel-index" aria-hidden="true">{{ pad2(item.index + 1) }}</span>
                  <span class="funnel-text">{{ item.card.question }}</span>
                  <el-icon class="funnel-arrow" aria-hidden="true"><TopRight /></el-icon>
                </button>
              </div>
            </div>
          </section>
        </div>
      </div>
    </transition>

    <main ref="messagesRef" class="qa-messages" @click="onMessageClick">
      <div v-if="messages.length === 0" class="qa-empty">
        <template v-if="faqCards.length === 0">
          <div class="qa-empty-badge">Y</div>
          <p class="qa-empty-text">选择知识库后即可开始提问</p>
        </template>
      </div>
      <div
        v-for="(message, index) in messages"
        :key="index"
        class="chat-item"
        :class="`chat-${message.role}`"
      >
        <div v-if="message.role === 'user'" class="avatar user-avatar">
          <el-icon><UserFilled /></el-icon>
        </div>
        <div v-else class="avatar assistant-avatar">
          <el-icon><Service /></el-icon>
        </div>
        <div v-if="message.role === 'user'" class="bubble user-bubble">
          <div class="bubble-text">{{ message.content }}</div>
        </div>
        <div v-else class="bubble assistant-bubble">
          <div
            v-if="message.content"
            class="bubble-text markdown-body"
            v-html="renderMarkdown(message.content)"
          />
          <div v-else-if="message.streaming" class="bubble-text bubble-muted bubble-streaming">
            正在生成…
          </div>
          <div v-else class="bubble-text bubble-muted">未获得回答，请查看错误提示。</div>
          <div v-if="message.clarification" class="clarify-box">
            <div class="clarify-question">{{ message.clarification.question }}</div>
            <div class="clarify-options">
              <el-button
                v-for="option in message.clarification.options"
                :key="option"
                size="small"
                round
                @click="ask(option)"
              >
                {{ option }}
              </el-button>
            </div>
          </div>
          <el-collapse v-if="message.sources.length > 0" class="source-collapse">
            <el-collapse-item :name="index">
              <template #title>
                <span class="source-title">引用来源（{{ message.sources.length }}）</span>
              </template>
              <div
                v-for="source in message.sources"
                :key="source.rank"
                class="source-item"
                @click="openSource(source)"
              >
                <div class="source-head">
                  <span class="source-rank">#{{ source.rank }}</span>
                  <span class="source-file">{{ source.source.filename }}</span>
                  <el-tag v-if="source.source.page_number !== null" size="small" type="info">
                    第 {{ source.source.page_number }} 页
                  </el-tag>
                  <el-tag v-if="source.source.section" size="small" type="info">
                    {{ source.source.section }}
                  </el-tag>
                </div>
                <div class="source-score">{{ scoreSummary(source) }}</div>
                <p class="source-preview">{{ previewText(source.content) }}</p>
              </div>
            </el-collapse-item>
          </el-collapse>
        </div>
      </div>
    </main>

    <footer class="qa-dock">
      <div class="dock-input">
        <textarea
          v-model="question"
          rows="1"
          maxlength="4000"
          placeholder="输入问题或描述任务..."
          :disabled="asking"
          @keydown.enter.exact.prevent="ask()"
        />
        <button
          class="dock-send"
          type="button"
          :disabled="asking || !selectedKbId || !question.trim()"
          @click="ask()"
        >
          <el-icon><Promotion /></el-icon>
        </button>
      </div>
      <div class="dock-controls">
        <el-select
          v-model="selectedCompanyId"
          placeholder="选择公司"
          size="small"
          style="width: 150px"
          @change="loadKbs"
        >
          <el-option
            v-for="company in activeCompanies"
            :key="company.id"
            :label="company.name"
            :value="company.id"
          />
        </el-select>
        <el-select v-model="selectedKbId" placeholder="选择知识库" size="small" style="width: 160px">
          <el-option v-for="kb in kbs" :key="kb.id" :label="kb.name" :value="kb.id" />
        </el-select>
        <span class="controls-divider"></span>
        <el-select
          v-model="productName"
          placeholder="产品名称（可选）"
          clearable
          filterable
          size="small"
          :disabled="!selectedKbId"
          style="width: 150px"
        >
          <el-option
            v-for="name in productOptions.product_names"
            :key="name"
            :label="name"
            :value="name"
          />
        </el-select>
        <el-select
          v-model="modelName"
          placeholder="产品型号（可选）"
          clearable
          filterable
          size="small"
          :disabled="!selectedKbId"
          style="width: 150px"
        >
          <el-option
            v-for="name in productOptions.model_names"
            :key="name"
            :label="name"
            :value="name"
          />
        </el-select>
        <span class="controls-divider"></span>
        <el-tooltip :content="`检索模式：${modeLabel}`" placement="top">
          <el-dropdown trigger="click" @command="setMode">
            <button class="ctrl-btn ctrl-active" type="button">
              <el-icon><Aim /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item
                  v-for="m in modeOptions"
                  :key="m.value"
                  :command="m.value"
                  :class="{ 'is-current': mode === m.value }"
                >
                  {{ m.label }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </el-tooltip>
        <el-tooltip :content="`Top K：${topK}`" placement="top">
          <el-dropdown trigger="click" @command="setTopK">
            <button class="ctrl-btn ctrl-active" type="button">
              <el-icon><List /></el-icon>
              <span class="ctrl-num">{{ topK }}</span>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item
                  v-for="n in topKOptions"
                  :key="n"
                  :command="n"
                  :class="{ 'is-current': topK === n }"
                >
                  {{ n }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </el-tooltip>
        <el-tooltip :content="includeContext ? '上下文扩展：开' : '上下文扩展：关'" placement="top">
          <button
            class="ctrl-btn"
            :class="{ 'ctrl-on': includeContext }"
            type="button"
            @click="includeContext = !includeContext"
          >
            <el-icon><Expand /></el-icon>
          </button>
        </el-tooltip>
        <el-tooltip content="新会话" placement="top">
          <button class="ctrl-btn" type="button" @click="startNewSession">
            <el-icon><RefreshRight /></el-icon>
          </button>
        </el-tooltip>
      </div>
    </footer>

    <el-dialog v-model="sourceDialogVisible" title="引用来源原文" width="640px">
      <template v-if="activeSource">
        <div class="source-dialog-head">
          <span class="source-rank">#{{ activeSource.rank }}</span>
          <span class="source-file">{{ activeSource.source.filename }}</span>
          <el-tag v-if="activeSource.source.page_number !== null" size="small" type="info">
            第 {{ activeSource.source.page_number }} 页
          </el-tag>
          <el-tag v-if="activeSource.source.section" size="small" type="info">
            {{ activeSource.source.section }}
          </el-tag>
          <span class="source-score">{{ scoreSummary(activeSource) }}</span>
        </div>
        <div class="source-dialog-content markdown-body" v-html="renderMarkdown(activeSource.content)" />
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 极简白布局：居中问候 + 推荐卡片 + 底部输入坞，沿用全局靛蓝主题。 */
.qa-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 104px);
  max-width: 920px;
  margin: 0 auto;
  padding: 0 20px;
}

.qa-intro {
  flex: none;
  display: grid;
  grid-template-rows: 1fr;
}
.qa-intro-inner {
  min-height: 0;
  overflow: hidden;
}
.intro-enter-active,
.intro-leave-active {
  transition: grid-template-rows 0.26s var(--kb-ease), opacity 0.2s var(--kb-ease),
    transform 0.22s var(--kb-ease);
}
.intro-enter-from,
.intro-leave-to {
  grid-template-rows: 0fr;
  opacity: 0;
  transform: translateY(-8px);
}

.qa-hero {
  position: relative;
  text-align: center;
  padding: 30px 0 18px;
}
.hero-title {
  margin: 0;
  font-size: 26px;
  font-weight: 600;
  letter-spacing: -0.3px;
  color: var(--el-text-color-primary);
}
.hero-sub {
  margin: 10px 0 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

/* 推荐问题：居中漏斗，顶端最宽、逐行收窄；SVG 勾出梯形边，内部用流光和呼吸感保持动态。 */
.faq-funnel {
  position: relative;
  isolation: isolate;
  box-sizing: border-box;
  width: min(100%, 680px);
  margin: 12px auto 4px;
  padding: 18px 22px 22px;
  animation: funnel-in 0.6s var(--kb-ease) backwards;
}
.faq-funnel.is-one {
  width: min(100%, 520px);
}
.faq-funnel.is-two {
  width: min(100%, 560px);
}
.funnel-aura {
  position: absolute;
  inset: 0;
  z-index: -1;
  overflow: hidden;
  clip-path: polygon(0 0, 100% 0, 84% 100%, 16% 100%);
  background:
    radial-gradient(70% 60% at 50% 0%, rgba(79, 70, 229, 0.075), transparent 72%),
    linear-gradient(180deg, rgba(79, 70, 229, 0.045), rgba(79, 70, 229, 0.012) 62%, transparent);
  animation: funnel-breathe 7.5s ease-in-out infinite;
}
.funnel-aura::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  width: 55%;
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.42), transparent);
  transform: translateX(-140%) skewX(-12deg);
  animation: funnel-sheen 8.5s ease-in-out 1.2s infinite;
}
.funnel-outline {
  position: absolute;
  inset: 0;
  z-index: -1;
  width: 100%;
  height: 100%;
  overflow: visible;
  pointer-events: none;
}
.funnel-outline path {
  fill: none;
  stroke: rgba(79, 70, 229, 0.22);
  stroke-width: 1;
  stroke-linejoin: round;
}
.funnel-head {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  padding-bottom: 15px;
  font-size: 11px;
  animation: funnel-head-in 0.5s var(--kb-ease) 0.05s backwards;
}
.funnel-dot {
  position: relative;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--el-color-primary);
}
.funnel-dot::after {
  content: '';
  position: absolute;
  inset: -4px;
  border-radius: 50%;
  border: 1px solid var(--el-color-primary-light-5);
  animation: funnel-pulse 2.6s ease-out infinite;
}
.funnel-eyebrow {
  font-weight: 500;
  letter-spacing: 0.16em;
  color: var(--el-text-color-secondary);
}
.funnel-count {
  padding: 1px 7px 2px;
  border: 1px solid rgba(79, 70, 229, 0.16);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.6);
  font-family: 'Cascadia Code', Consolas, 'Courier New', monospace;
  font-size: 10px;
  line-height: 1.4;
  letter-spacing: 0.06em;
  font-variant-numeric: tabular-nums;
  color: var(--el-color-primary);
}
.funnel-rows {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 9px;
}
.funnel-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 9px;
  width: var(--row-w);
  animation: funnel-row-in 0.55s var(--kb-ease) backwards;
  animation-delay: calc(90ms + var(--r) * 90ms);
}
.funnel-row.is-single {
  grid-template-columns: minmax(0, 1fr);
}
.funnel-item {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 9px;
  min-height: 42px;
  padding: 8px 12px 8px 11px;
  border: 1px solid rgba(15, 23, 42, 0.07);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.68);
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.025);
  text-align: left;
  cursor: pointer;
  font-family: inherit;
  backdrop-filter: blur(6px);
  animation: funnel-item-in 0.5s var(--kb-ease) backwards;
  animation-delay: calc(150ms + var(--i) * 55ms);
  transition:
    transform 0.2s var(--kb-ease),
    border-color 0.2s var(--kb-ease),
    background-color 0.2s var(--kb-ease),
    box-shadow 0.2s var(--kb-ease);
}
.funnel-item:hover {
  transform: translateY(-2px);
  border-color: rgba(79, 70, 229, 0.28);
  background: rgba(255, 255, 255, 0.94);
  box-shadow: 0 10px 22px -16px rgba(49, 46, 129, 0.55);
}
.funnel-item:focus-visible {
  outline: 2px solid var(--el-color-primary-light-5);
  outline-offset: 2px;
}
.funnel-item:active {
  transform: translateY(0) scale(0.992);
  transition-duration: 0.08s;
}
.funnel-index {
  font-family: 'Cascadia Code', Consolas, 'Courier New', monospace;
  font-size: 10px;
  line-height: 1;
  letter-spacing: 0.04em;
  font-variant-numeric: tabular-nums;
  color: var(--el-text-color-placeholder);
  transition: color 0.2s var(--kb-ease);
}
.funnel-text {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  font-size: 13px;
  line-height: 1.55;
  letter-spacing: 0.01em;
  color: var(--el-text-color-regular);
  transition: color 0.2s var(--kb-ease);
}
.funnel-arrow {
  font-size: 12px;
  color: var(--el-color-primary);
  opacity: 0;
  transform: translate(-4px, 4px);
  transition:
    opacity 0.2s var(--kb-ease),
    transform 0.2s var(--kb-ease);
}
.funnel-item:hover .funnel-index,
.funnel-item:focus-visible .funnel-index {
  color: var(--el-color-primary);
}
.funnel-item:hover .funnel-text,
.funnel-item:focus-visible .funnel-text {
  color: var(--el-text-color-primary);
}
.funnel-item:hover .funnel-arrow,
.funnel-item:focus-visible .funnel-arrow {
  opacity: 1;
  transform: translate(0, 0);
}

@keyframes funnel-in {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
@keyframes funnel-head-in {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
@keyframes funnel-row-in {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
@keyframes funnel-item-in {
  from {
    opacity: 0;
    transform: translateY(7px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
@keyframes funnel-sheen {
  0% {
    opacity: 0;
    transform: translateX(-140%) skewX(-12deg);
  }
  14% {
    opacity: 0.8;
  }
  52% {
    opacity: 0;
    transform: translateX(280%) skewX(-12deg);
  }
  100% {
    opacity: 0;
    transform: translateX(280%) skewX(-12deg);
  }
}
@keyframes funnel-breathe {
  0%,
  100% {
    opacity: 0.72;
  }
  50% {
    opacity: 1;
  }
}
@keyframes funnel-pulse {
  0% {
    opacity: 0.7;
    transform: scale(0.6);
  }
  70%,
  100% {
    opacity: 0;
    transform: scale(1.55);
  }
}

@media (max-width: 680px) {
  .faq-funnel {
    padding: 16px 14px 18px;
  }
  .funnel-row {
    gap: 8px;
  }
  .funnel-item {
    gap: 7px;
    padding: 8px 10px;
  }
  .funnel-arrow {
    display: none;
  }
}
@media (prefers-reduced-motion: reduce) {
  .faq-funnel,
  .funnel-aura,
  .funnel-aura::after,
  .funnel-head,
  .funnel-row,
  .funnel-item,
  .funnel-dot::after {
    animation: none;
  }
  .funnel-aura::after {
    opacity: 0;
  }
  .funnel-item,
  .funnel-index,
  .funnel-text,
  .funnel-arrow {
    transition: none;
  }
}

.qa-messages {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 10px 2px;
}
.qa-empty {
  padding-top: 40px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}
.qa-empty-badge {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  color: var(--el-color-primary);
  font-weight: 600;
  font-size: 18px;
  background: var(--el-color-primary-light-9);
  animation: empty-float 2.8s ease-in-out infinite;
}
@keyframes empty-float {
  0%,
  100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-4px);
  }
}
.qa-empty-text {
  margin: 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

/* 底部输入坞：白色容器 + 实心靛蓝发送键。 */
.qa-dock {
  flex: none;
  background: #fff;
  border: 1px solid var(--el-border-color);
  border-radius: 12px;
  box-shadow: 0 4px 16px rgba(15, 23, 42, 0.05);
  padding: 12px 14px 10px;
  margin-bottom: 14px;
}
.dock-input {
  display: flex;
  gap: 10px;
  align-items: flex-end;
}
.dock-input textarea {
  flex: 1;
  border: none;
  outline: none;
  resize: none;
  background: transparent;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.6;
  color: var(--el-text-color-primary);
  padding: 6px 2px;
  min-height: 26px;
}
.dock-input textarea::placeholder {
  color: var(--el-text-color-placeholder);
}
.dock-send {
  width: 36px;
  height: 36px;
  flex: none;
  border: none;
  border-radius: var(--el-border-radius-small);
  display: grid;
  place-items: center;
  color: #fff;
  font-size: 16px;
  cursor: pointer;
  background: var(--el-color-primary);
  transition: background-color 0.15s var(--kb-ease), transform 0.15s var(--kb-ease),
    opacity 0.15s var(--kb-ease);
}
.dock-send:hover:not(:disabled) {
  background: var(--el-color-primary-dark-2);
}
.dock-send:active:not(:disabled) {
  transform: scale(0.97);
}
.dock-send:disabled {
  opacity: 0.45;
  cursor: not-allowed;
  box-shadow: none;
}
.dock-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 10px;
}
.controls-divider {
  width: 1px;
  height: 18px;
  margin: 0 4px;
  background: var(--el-border-color);
}
.ctrl-btn {
  height: 28px;
  min-width: 28px;
  padding: 0 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-small);
  background: #fff;
  color: var(--el-text-color-regular);
  font-size: 13px;
  cursor: pointer;
  transition: color 0.15s var(--kb-ease), border-color 0.15s var(--kb-ease),
    background-color 0.15s var(--kb-ease), transform 0.15s var(--kb-ease);
}
.ctrl-btn:hover {
  border-color: var(--el-color-primary-light-5);
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.ctrl-btn:active {
  transform: scale(0.97);
}
.ctrl-active {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary-light-7);
  background: var(--el-color-primary-light-9);
}
.ctrl-on {
  color: #fff;
  border-color: transparent;
  background: var(--el-color-primary);
}
.ctrl-on:hover {
  color: #fff;
  background: var(--el-color-primary-dark-2);
}
.ctrl-num {
  font-size: 12px;
  font-weight: 600;
}
.el-dropdown-menu__item.is-current {
  color: var(--el-color-primary);
  font-weight: 600;
}

.chat-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 16px;
}
.chat-user {
  justify-content: flex-end;
}
.chat-user .avatar {
  order: 2;
}
.chat-user .bubble {
  order: 1;
}
.chat-assistant {
  justify-content: flex-start;
}
.avatar {
  width: 34px;
  height: 34px;
  border-radius: 50%;
  flex: none;
  display: grid;
  place-items: center;
  color: #fff;
  font-size: 15px;
}
.user-avatar {
  background: #1f2329;
}
.assistant-avatar {
  background: var(--el-color-primary);
}
.bubble {
  max-width: 52%;
  padding: 12px 16px;
  border-radius: 10px;
  animation: bubble-in 0.2s var(--kb-ease) both;
}
.user-bubble {
  background: var(--el-color-primary);
  color: #fff;
  border-bottom-right-radius: 4px;
}
.user-bubble .bubble-text {
  color: #fff;
}
.assistant-bubble {
  background: #fff;
  border: 1px solid var(--el-border-color);
  border-bottom-left-radius: 4px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}
@keyframes bubble-in {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
.bubble-text {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.7;
  font-size: 14px;
}
.bubble-muted {
  color: var(--el-text-color-secondary);
}
.bubble-streaming {
  animation: stream-pulse 1.2s ease-in-out infinite;
}
@keyframes stream-pulse {
  0%,
  100% {
    opacity: 0.45;
  }
  50% {
    opacity: 1;
  }
}
.clarify-box {
  margin-top: 10px;
  padding: 10px;
  background: var(--el-color-warning-light-9);
  border: 1px solid var(--el-color-warning-light-8);
  border-radius: var(--el-border-radius-base);
}
.clarify-question {
  font-size: 13px;
  margin-bottom: 6px;
}
.clarify-options {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.clarify-options .el-button {
  border-radius: var(--el-border-radius-small);
}
.source-collapse {
  margin-top: 8px;
}
.source-title {
  font-size: 13px;
  color: var(--el-color-primary);
  font-weight: 600;
}
.source-item {
  border-top: 1px solid var(--el-border-color-lighter);
  padding: 8px 10px;
  cursor: pointer;
  border-radius: var(--el-border-radius-small);
  transition: background-color 0.15s var(--kb-ease);
}
.source-item:hover {
  background: var(--el-fill-color-light);
}
.source-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.source-rank {
  font-weight: 600;
  color: var(--el-color-primary);
}
.source-file {
  font-size: 13px;
}
.source-score {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 4px 0;
  font-variant-numeric: tabular-nums;
}
.source-preview {
  margin: 4px 0 0;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--el-text-color-regular);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.source-dialog-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.source-dialog-content {
  word-break: break-word;
  line-height: 1.7;
  max-height: 50vh;
  overflow-y: auto;
  background: var(--el-fill-color-light);
  border-radius: var(--el-border-radius-base);
  padding: 12px;
}

/* 答案正文由 v-html 注入，元素不带 scoped 属性，需 :deep() 穿透。 */
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
:deep(.markdown-body pre) {
  background: var(--el-fill-color-light);
  border-radius: var(--el-border-radius-base);
  padding: 10px 12px;
  overflow: auto;
}
:deep(.markdown-body pre code) {
  background: transparent;
  padding: 0;
}
:deep(.markdown-body img.md-image) {
  display: inline-block;
  max-width: 100%;
  max-height: 260px;
  border-radius: var(--el-border-radius-base);
  border: 1px solid var(--el-border-color);
  margin: 6px 4px 2px 0;
  vertical-align: middle;
  cursor: zoom-in;
}
:deep(.markdown-body img.md-image:hover) {
  opacity: 0.9;
}
:deep(.markdown-body table) {
  border-collapse: collapse;
  margin: 8px 0;
  width: 100%;
  font-size: 13px;
}
:deep(.markdown-body th),
:deep(.markdown-body td) {
  border: 1px solid var(--el-border-color);
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
