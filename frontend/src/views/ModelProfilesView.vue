<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { client, errorMessage } from '../api/client'

interface ModelPreset {
  key: string
  display_name: string
  base_url: string
  embedding_path: string
  supports_embedding: boolean
  supports_chat: boolean
  chat_path: string | null
  known_chat_models: string[]
  note: string | null
  known_models: string[]
  supports_rerank: boolean
  rerank_path: string | null
  rerank_base_url: string | null
  known_rerank_models: string[]
  supports_ocr: boolean
  ocr_path: string | null
  ocr_base_url: string | null
  ocr_mode: 'auto' | 'vision' | 'paddleocr' | 'baidu_async' | null
  known_ocr_models: string[]
}

type ProfileKind = 'embedding' | 'rerank' | 'chat' | 'ocr'
type OcrMode = 'auto' | 'vision' | 'paddleocr' | 'baidu_async'

interface ModelProfile {
  id: string
  name: string
  kind: ProfileKind
  provider: string
  model_name: string
  dimensions: number | null
  settings: Record<string, unknown>
  secret_ref: string | null
  is_enabled: boolean
  is_default: boolean
  has_api_key: boolean
  created_at: string
  updated_at: string
}

interface ProfileTestResult {
  ok: boolean
  kind: ProfileKind
  latency_ms: number
  message: string
  error_code: string | null
}

type UsageSlotKey = 'embedding' | 'rerank' | 'chat' | 'ocr'
type UsageSource = 'knowledge_base' | 'default' | 'environment'

interface ModelUsageItem {
  profile_id: string | null
  name: string
  provider: string
  model_name: string
  source: UsageSource
  scope_names: string[]
  is_default: boolean
  is_active: boolean
  is_enabled: boolean
}

interface ModelUsageSlot {
  key: UsageSlotKey
  items: ModelUsageItem[]
}

const SLOT_META: Record<UsageSlotKey, { label: string; desc: string; empty: string }> = {
  embedding: {
    label: '文档向量化与检索',
    desc: '文档入库切片向量化、在线提问向量化、FAQ 问题匹配',
    empty: '暂无生效的 Embedding 模型，新建知识库将无法入库。',
  },
  rerank: {
    label: '检索结果重排',
    desc: '混合检索候选按相关度重排，子库未绑定时跳过',
    empty: '没有子库绑定 Rerank 模型，检索结果按原顺序返回。',
  },
  chat: {
    label: '问答与 FAQ 生成 / 改写',
    desc: '在线问答答案生成、FAQ 自动生成、FAQ AI 改写',
    empty: '暂无可用生成模型，问答与 FAQ 生成、改写均不可用。',
  },
  ocr: {
    label: '扫描件与图片识别',
    desc: '扫描版 PDF 与 Office 内嵌图片的文字识别',
    empty: '未配置 OCR 模型，扫描件与图片无法提取文字。',
  },
}

const SOURCE_META: Record<UsageSource, { label: string; type: 'primary' | 'warning' | 'info' }> = {
  knowledge_base: { label: '知识库绑定', type: 'primary' },
  default: { label: '默认档案', type: 'warning' },
  environment: { label: '环境配置', type: 'info' },
}

const KIND_USAGE: Record<ProfileKind, string> = {
  embedding: '入库 / 检索 / FAQ 匹配',
  rerank: '检索重排',
  chat: '问答 / FAQ 生成改写',
  ocr: '扫描件 / 图片识别',
}

const OCR_MODE_OPTIONS: { value: OcrMode; label: string }[] = [
  { value: 'auto', label: '自动识别（按模型名）' },
  { value: 'vision', label: '视觉对话接口' },
  { value: 'paddleocr', label: 'PaddleOCR 同步接口' },
  { value: 'baidu_async', label: '百度官方异步任务' },
]

const OCR_MODE_LABELS: Record<OcrMode, string> = {
  auto: '自动识别',
  vision: '视觉对话',
  paddleocr: 'PaddleOCR 同步',
  baidu_async: '百度异步任务',
}

interface ProfileForm {
  name: string
  kind: ProfileKind
  provider: string
  model_name: string
  dimensions: number | null
  base_url: string
  path: string
  ocr_mode: OcrMode
  batch_size: number
  secret_ref: string
  api_key: string
  is_enabled: boolean
  // 编辑时保留表单未覆盖的档案参数（如异步 OCR 的轮询与版面选项）。
  extraSettings: Record<string, unknown>
}

interface ModelPrice {
  id: string
  model_name: string
  provider: string | null
  input_price_per_million: number
  output_price_per_million: number
  updated_at: string
}

const route = useRoute()
const activeTab = ref(route.query.tab === 'prices' ? 'prices' : 'usage')
const presets = ref<ModelPreset[]>([])
const profiles = ref<ModelProfile[]>([])
const usageSlots = ref<ModelUsageSlot[]>([])
const loading = ref(false)
const usageLoading = ref(false)
const presetLoading = ref(false)

const prices = ref<ModelPrice[]>([])
const priceLoading = ref(false)
const priceDialogVisible = ref(false)
const priceSubmitting = ref(false)
// 记录编辑前的模型名：模型名是核算主键，改名等于新增，因此编辑时锁定。
const priceEditingName = ref<string | null>(null)
const priceForm = reactive({
  model_name: '',
  provider: '',
  input_price: '',
  output_price: '',
})

const dialogVisible = ref(false)
const editingId = ref<string | null>(null)
const submitting = ref(false)
const promoteDefault = ref(false)
const form = reactive<ProfileForm>(emptyForm())

const keyDialogVisible = ref(false)
const keyProfile = ref<ModelProfile | null>(null)
const apiKey = ref('')
const keySubmitting = ref(false)
const testingId = ref<string | null>(null)

const providerOptions = computed(() =>
  presets.value
    .filter((preset) => supportsKind(preset, form.kind))
    .map((preset) => ({ value: preset.key, label: preset.display_name })),
)

const editingProfileHasKey = computed(
  () => profiles.value.find((profile) => profile.id === editingId.value)?.has_api_key ?? false,
)

const secretRefOptions = ['dashscope', 'bigmodel', 'volcengine', 'deepseek', 'qianfan'].map(
  (value) => ({ value, label: value }),
)

const modelNamePlaceholder = computed(() => {
  if (form.kind === 'chat') return '供应商控制台中的生成模型名'
  if (form.kind === 'ocr') return '例如 qwen-vl-ocr、paddleocr-vl-0.9b 或 Endpoint ID'
  return '供应商控制台中的模型名或 Endpoint ID'
})

const pathLabel = computed(
  () =>
    ({
      embedding: 'Embedding 路径',
      chat: 'Chat 路径',
      rerank: 'Rerank 路径',
      ocr: '视觉接口路径',
    })[form.kind],
)

function supportsKind(preset: ModelPreset, kind: ProfileKind): boolean {
  if (kind === 'embedding') return preset.supports_embedding
  if (kind === 'chat') return preset.supports_chat
  if (kind === 'rerank') return preset.supports_rerank
  return preset.supports_ocr
}

function emptyForm(): ProfileForm {
  return {
    name: '',
    kind: 'embedding',
    provider: 'dashscope',
    model_name: '',
    dimensions: null,
    base_url: '',
    path: '/embeddings',
    ocr_mode: 'auto',
    batch_size: 1,
    secret_ref: '',
    api_key: '',
    is_enabled: true,
    extraSettings: {},
  }
}

async function loadPresets() {
  presetLoading.value = true
  try {
    presets.value = (await client.get('/admin/model-presets')).data
  } catch (error) {
    errorMessage(error, '加载厂商预设失败')
  } finally {
    presetLoading.value = false
  }
}

async function loadProfiles() {
  loading.value = true
  try {
    profiles.value = (await client.get('/admin/model-profiles')).data
  } catch (error) {
    errorMessage(error, '加载模型档案失败')
  } finally {
    loading.value = false
  }
}

async function loadUsage() {
  usageLoading.value = true
  try {
    usageSlots.value = (await client.get('/admin/model-usage')).data.slots
  } catch (error) {
    errorMessage(error, '加载模型调用位置失败')
  } finally {
    usageLoading.value = false
  }
}

async function loadPrices() {
  priceLoading.value = true
  try {
    prices.value = (await client.get('/admin/model-prices')).data
  } catch (error) {
    errorMessage(error, '加载计费单价失败')
  } finally {
    priceLoading.value = false
  }
}

async function refreshAll() {
  await Promise.all([loadProfiles(), loadUsage(), loadPrices()])
}

// 当前生成链路实际用到的模型里还没配单价的，给出快捷添加入口。
const unpricedChatModels = computed(() => {
  const seen = new Set(prices.value.map((price) => price.model_name))
  const items: ModelUsageItem[] = []
  const slot = usageSlots.value.find((item) => item.key === 'chat')
  for (const item of slot?.items ?? []) {
    if (!item.model_name.trim() || seen.has(item.model_name)) continue
    seen.add(item.model_name)
    items.push(item)
  }
  return items
})

function formatPrice(value: number): string {
  return Number(value.toFixed(6)).toString()
}

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

function openPriceCreate(modelName = '', provider = '') {
  priceEditingName.value = null
  Object.assign(priceForm, {
    model_name: modelName,
    provider,
    input_price: '',
    output_price: '',
  })
  priceDialogVisible.value = true
}

function openPriceEdit(price: ModelPrice) {
  priceEditingName.value = price.model_name
  Object.assign(priceForm, {
    model_name: price.model_name,
    provider: price.provider ?? '',
    input_price: formatPrice(price.input_price_per_million),
    output_price: formatPrice(price.output_price_per_million),
  })
  priceDialogVisible.value = true
}

function parsePrice(value: string): number | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  const parsed = Number(trimmed)
  if (!Number.isFinite(parsed) || parsed < 0) return null
  return Math.round(parsed * 1_000_000) / 1_000_000
}

async function submitPrice() {
  const modelName = priceForm.model_name.trim()
  if (!modelName) {
    ElMessage.warning('请填写模型名')
    return
  }
  const input = parsePrice(priceForm.input_price)
  const output = parsePrice(priceForm.output_price)
  if (input == null || output == null) {
    ElMessage.warning('单价需为不小于 0 的数字，最多 6 位小数')
    return
  }
  priceSubmitting.value = true
  try {
    await client.put('/admin/model-prices', {
      model_name: modelName,
      provider: priceForm.provider.trim() || null,
      input_price_per_million: input,
      output_price_per_million: output,
    })
    ElMessage.success('单价已保存')
    priceDialogVisible.value = false
    await loadPrices()
  } catch (error) {
    errorMessage(error, '保存单价失败')
  } finally {
    priceSubmitting.value = false
  }
}

async function deletePrice(price: ModelPrice) {
  try {
    await ElMessageBox.confirm(
      `删除「${price.model_name}」的单价后，该模型在仪表盘中不再计入成本金额。`,
      '删除单价',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/admin/model-prices/${price.id}`)
    ElMessage.success('已删除')
    await loadPrices()
  } catch (error) {
    errorMessage(error, '删除失败')
  }
}

function kindUsage(kind: string): string {
  return KIND_USAGE[kind as keyof typeof KIND_USAGE] ?? ''
}

function usageStatus(item: ModelUsageItem) {
  if (!item.model_name.trim()) return { label: '未配置', type: 'info' as const }
  if (!item.is_enabled) return { label: '已停用', type: 'info' as const }
  if (!item.is_active) return { label: '未生效', type: 'info' as const }
  return { label: '生效中', type: 'success' as const }
}

function scopeLabel(item: ModelUsageItem): string {
  if (!item.model_name.trim()) return '说明'
  return item.scope_names.length ? '用于' : '范围'
}

function scopeText(item: ModelUsageItem, slotKey: UsageSlotKey): string {
  if (!item.model_name.trim()) return '未配置，可在模型档案中新建并在 .env 补充环境兜底'
  if (item.scope_names.length > 3) {
    return `${item.scope_names.slice(0, 3).join('、')} 等 ${item.scope_names.length} 个子库`
  }
  if (item.scope_names.length) return item.scope_names.join('、')
  if (item.source === 'environment') {
    if (slotKey === 'chat') return '默认 Chat 档案缺失时全局兜底'
    if (slotKey === 'ocr') return '默认 OCR 档案缺失时全局兜底'
    return '服务全局'
  }
  if (item.source === 'default') {
    return slotKey === 'chat' || slotKey === 'ocr' ? '全局生效' : '新建知识库时沿用'
  }
  return ''
}

function applyPreset(provider: string) {
  const preset = presets.value.find((item) => item.key === provider)
  if (!preset) return
  if (form.kind === 'embedding') {
    form.base_url = preset.base_url
    form.path = preset.embedding_path
  } else if (form.kind === 'chat') {
    form.base_url = preset.base_url
    form.path = preset.chat_path ?? '/chat/completions'
  } else if (form.kind === 'rerank') {
    form.base_url = preset.rerank_base_url ?? preset.base_url
    form.path = preset.rerank_path ?? '/reranks'
  } else {
    form.base_url = preset.ocr_base_url ?? preset.base_url
    form.path = preset.ocr_path ?? '/chat/completions'
    form.ocr_mode = preset.ocr_mode ?? 'auto'
  }
}

function switchKind() {
  form.dimensions = null
  form.batch_size = 1
  form.ocr_mode = 'auto'
  form.extraSettings = {}
  const current = presets.value.find((item) => item.key === form.provider)
  if (!current || !supportsKind(current, form.kind)) {
    // 切换类型后优先保留当前厂商；不支持时回落到 dashscope 或首个可用厂商。
    form.provider =
      presets.value.find(
        (item) => item.key === 'dashscope' && supportsKind(item, form.kind),
      )?.key ??
      presets.value.find((item) => supportsKind(item, form.kind))?.key ??
      form.provider
  }
  applyPreset(form.provider)
}

function openCreate() {
  Object.assign(form, emptyForm())
  editingId.value = null
  promoteDefault.value = false
  applyPreset(form.provider)
  dialogVisible.value = true
}

function onOcrModeChange(mode: OcrMode) {
  // 异步任务固定走百度 AIP，避免沿用千帆 v2 基址导致 404。
  if (mode === 'baidu_async' && !form.base_url.includes('aip.baidubce.com')) {
    form.base_url = 'https://aip.baidubce.com'
  }
}

function ocrModeLabel(value: string | null): string {
  if (!value) return '—'
  return OCR_MODE_LABELS[value as OcrMode] ?? value
}

function openEdit(profile: ModelProfile) {
  editingId.value = profile.id
  promoteDefault.value = false
  const settings = { ...(profile.settings ?? {}) }
  const extraSettings = { ...settings }
  for (const key of [
    'base_url',
    'embedding_path',
    'batch_size',
    'chat_path',
    'rerank_path',
    'ocr_mode',
  ]) {
    delete extraSettings[key]
  }
  Object.assign(form, {
    name: profile.name,
    kind: profile.kind,
    provider: profile.provider,
    model_name: profile.model_name,
    dimensions: profile.dimensions,
    base_url: String(settings.base_url ?? ''),
    path:
      profile.kind === 'embedding'
        ? String(settings.embedding_path ?? '/embeddings')
        : profile.kind === 'chat'
          ? String(settings.chat_path ?? '/chat/completions')
          : profile.kind === 'ocr'
            ? String(settings.chat_path ?? '/chat/completions')
            : String(settings.rerank_path ?? '/reranks'),
    ocr_mode: (String(settings.ocr_mode ?? 'auto') as OcrMode) || 'auto',
    batch_size: Number(settings.batch_size ?? 1),
    secret_ref: profile.secret_ref ?? '',
    api_key: '',
    is_enabled: profile.is_enabled,
    extraSettings,
  })
  dialogVisible.value = true
}

function buildPayload() {
  const settings: Record<string, unknown> = {
    ...form.extraSettings,
    base_url: form.base_url,
  }
  if (form.kind === 'embedding') {
    settings.embedding_path = form.path
    settings.batch_size = form.batch_size
  } else if (form.kind === 'chat') {
    settings.chat_path = form.path
  } else if (form.kind === 'ocr') {
    settings.chat_path = form.path
    settings.ocr_mode = form.ocr_mode
  } else {
    settings.rerank_path = form.path
  }
  return {
    name: form.name,
    provider: form.provider,
    model_name: form.model_name,
    dimensions: form.kind === 'embedding' ? form.dimensions : null,
    settings,
    secret_ref: form.secret_ref.trim() ? form.secret_ref.trim() : null,
    is_enabled: form.is_enabled,
  }
}

async function submitProfile() {
  submitting.value = true
  try {
    let profileId = editingId.value
    if (profileId) {
      await client.patch(`/admin/model-profiles/${profileId}`, buildPayload())
    } else {
      const { data } = await client.post<ModelProfile>('/admin/model-profiles', {
        ...buildPayload(),
        kind: form.kind,
      })
      profileId = data.id
    }
    const key = form.api_key.trim()
    if (key) {
      // 密钥只写不回读；档案已保存时即使密钥失败也保留档案，提示稍后补配。
      try {
        await client.put(`/admin/model-profiles/${profileId}/api-key`, { api_key: key })
      } catch {
        ElMessage.warning('档案已保存，但 API Key 写入失败，请在列表中重新配置')
      }
    }
    if (promoteDefault.value && profileId) {
      await client.post(`/admin/model-profiles/${profileId}/default`)
    }
    ElMessage.success(editingId.value ? '模型档案已更新' : '模型档案已创建')
    dialogVisible.value = false
    await refreshAll()
  } catch (error) {
    errorMessage(error, '保存模型档案失败')
  } finally {
    submitting.value = false
  }
}

async function testProfile(profile: ModelProfile) {
  testingId.value = profile.id
  try {
    // 真实发起一次最小调用；失败也返回 200，用 ok 区分结果。
    const { data } = await client.post<ProfileTestResult>(
      `/admin/model-profiles/${profile.id}/test`,
    )
    if (data.ok) {
      ElMessage.success(`连通正常（${data.latency_ms} ms）：${data.message}`)
    } else {
      const code = data.error_code ? `${data.error_code} · ` : ''
      ElMessage.error(`连通失败（${data.latency_ms} ms）：${code}${data.message}`)
    }
  } catch (error) {
    errorMessage(error, '测试连通性失败')
  } finally {
    testingId.value = null
  }
}

async function toggleEnabled(profile: ModelProfile, value: string | number | boolean) {
  try {
    // PATCH 只提交单字段，服务端与当前值合并后整体校验。
    await client.patch(`/admin/model-profiles/${profile.id}`, { is_enabled: Boolean(value) })
    profile.is_enabled = Boolean(value)
    await loadUsage()
  } catch (error) {
    errorMessage(error, '切换启用状态失败')
    await loadProfiles()
  }
}

async function setDefault(profile: ModelProfile) {
  try {
    await client.post(`/admin/model-profiles/${profile.id}/default`)
    await refreshAll()
  } catch (error) {
    errorMessage(error, '设为默认失败')
  }
}

async function clearDefault(profile: ModelProfile) {
  try {
    await client.delete(`/admin/model-profiles/${profile.id}/default`)
    await refreshAll()
  } catch (error) {
    errorMessage(error, '取消默认失败')
  }
}

async function removeProfile(profile: ModelProfile) {
  try {
    await ElMessageBox.confirm(
      `确定删除模型档案「${profile.name}」？已被知识库绑定的档案无法删除。`,
      '删除模型档案',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/admin/model-profiles/${profile.id}`)
    ElMessage.success('已删除')
    await refreshAll()
  } catch (error) {
    errorMessage(error, '删除失败')
  }
}

function openKeyDialog(profile: ModelProfile) {
  keyProfile.value = profile
  apiKey.value = ''
  keyDialogVisible.value = true
}

async function submitApiKey() {
  if (!keyProfile.value) return
  const value = apiKey.value.trim()
  if (!value) {
    ElMessage.warning('请输入 API Key')
    return
  }
  keySubmitting.value = true
  try {
    // 只写不回读；成功后列表仅刷新"已配置"状态。
    await client.put(`/admin/model-profiles/${keyProfile.value.id}/api-key`, { api_key: value })
    ElMessage.success('API Key 已保存')
    keyDialogVisible.value = false
    await loadProfiles()
  } catch (error) {
    errorMessage(error, '保存 API Key 失败')
  } finally {
    keySubmitting.value = false
  }
}

async function clearApiKey(profile: ModelProfile) {
  try {
    await ElMessageBox.confirm(
      `确定清除「${profile.name}」的 API Key？清除后该档案将回落到环境密钥引用。`,
      '清除 API Key',
      { type: 'warning', confirmButtonText: '清除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/admin/model-profiles/${profile.id}/api-key`)
    ElMessage.success('已清除')
    await loadProfiles()
  } catch (error) {
    errorMessage(error, '清除失败')
  }
}

function settingsText(profile: ModelProfile): string {
  const settings = profile.settings ?? {}
  return [
    settings.base_url,
    settings.embedding_path ?? settings.rerank_path ?? settings.chat_path,
  ]
    .filter(Boolean)
    .join(' ')
}

function editUsageModel(item: ModelUsageItem) {
  const profile = profiles.value.find((candidate) => candidate.id === item.profile_id)
  if (!profile) {
    ElMessage.info('档案列表正在刷新，请稍后再试')
    loadProfiles()
    return
  }
  openEdit(profile)
}

function adoptEnvironmentModel(slotKey: UsageSlotKey, item: ModelUsageItem) {
  Object.assign(form, emptyForm())
  editingId.value = null
  form.kind = slotKey
  form.provider = item.provider || 'dashscope'
  form.model_name = item.model_name
  // 环境配置的 provider 即 secret_ref 约定名，预填后可直接复用 .env 中的密钥。
  form.secret_ref = item.provider
  form.name =
    slotKey === 'chat' ? '默认生成模型' : slotKey === 'ocr' ? '默认 OCR 模型' : ''
  promoteDefault.value = true
  applyPreset(form.provider)
  dialogVisible.value = true
}

onMounted(() => {
  loadPresets()
  refreshAll()
})
</script>

<template>
  <el-card>
    <template #header>
      <div class="header">
        <span class="header-title"><el-icon><Cpu /></el-icon>模型管理</span>
        <el-button :loading="loading || usageLoading" @click="refreshAll()">刷新</el-button>
      </div>
    </template>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="调用位置" name="usage">
        <p class="usage-head">
          汇总服务实际调用的全部模型；档案可直接编辑，环境配置项可新建默认档案替换，无需重启。
        </p>
        <div v-loading="usageLoading" class="usage-list">
          <div v-for="slot in usageSlots" :key="slot.key" class="usage-row">
            <div class="usage-slot">
              <div class="usage-slot-title">{{ SLOT_META[slot.key].label }}</div>
              <div class="usage-slot-desc">{{ SLOT_META[slot.key].desc }}</div>
            </div>
            <div class="usage-models">
              <div
                v-for="item in slot.items"
                :key="`${slot.key}-${item.profile_id ?? item.source}`"
                class="usage-model"
                :class="{ 'is-inactive': usageStatus(item).label !== '生效中' }"
              >
                <div class="usage-model-main">
                  <span class="usage-model-name">{{ item.name }}</span>
                  <el-tag size="small" effect="plain" :type="SOURCE_META[item.source].type">
                    {{ SOURCE_META[item.source].label }}
                  </el-tag>
                  <el-tag v-if="item.is_default" size="small" type="danger" effect="plain">
                    默认
                  </el-tag>
                  <el-tag size="small" effect="plain" :type="usageStatus(item).type">
                    {{ usageStatus(item).label }}
                  </el-tag>
                  <span class="usage-model-actions">
                    <el-button
                      v-if="item.profile_id"
                      link
                      type="primary"
                      size="small"
                      @click="editUsageModel(item)"
                    >
                      编辑
                    </el-button>
                    <el-button
                      v-else
                      link
                      type="primary"
                      size="small"
                      @click="adoptEnvironmentModel(slot.key, item)"
                    >
                      接入管理
                    </el-button>
                  </span>
                </div>
                <div class="usage-model-meta">
                  <span>{{ item.provider || '未指定供应商' }}</span>
                  <span class="meta-dot">·</span>
                  <span class="usage-model-id">{{ item.model_name || '未配置模型名' }}</span>
                </div>
                <div v-if="scopeText(item, slot.key)" class="usage-model-scope">
                  <span class="scope-label">{{ scopeLabel(item) }}</span>
                  {{ scopeText(item, slot.key) }}
                </div>
              </div>
              <div v-if="!slot.items.length" class="usage-empty">
                {{ SLOT_META[slot.key].empty }}
              </div>
            </div>
          </div>
          <div v-if="!usageSlots.length && !usageLoading" class="usage-empty">
            暂无模型调用信息。
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="模型档案" name="profiles">
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="模型档案在大库创建时锁定；已被大库/子库绑定的档案不可修改或删除，换模型需新建大库。"
          class="tip"
        />
        <div class="toolbar">
          <el-button type="primary" @click="openCreate">新建档案</el-button>
        </div>
        <el-table v-loading="loading" :data="profiles" border>
          <el-table-column prop="name" label="名称" min-width="160" />
          <el-table-column label="类型与用途" width="160">
            <template #default="{ row }">
              <div class="kind-cell">
                <el-tag v-if="row.kind === 'embedding'" type="info">Embedding</el-tag>
                <el-tag v-else-if="row.kind === 'chat'" type="success">Chat 生成</el-tag>
                <el-tag v-else-if="row.kind === 'ocr'" type="primary">OCR</el-tag>
                <el-tag v-else type="warning">Rerank</el-tag>
                <span class="kind-usage">{{ kindUsage(row.kind) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="provider" label="服务商" width="130" />
          <el-table-column prop="model_name" label="模型名" min-width="160" />
          <el-table-column prop="dimensions" label="维度" width="80" />
          <el-table-column label="地址与路径" min-width="220">
            <template #default="{ row }">{{ settingsText(row) }}</template>
          </el-table-column>
          <el-table-column label="API Key" width="110">
            <template #default="{ row }">
              <el-tag :type="row.has_api_key ? 'success' : 'info'">
                {{ row.has_api_key ? '已配置' : '未配置' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="启用" width="70">
            <template #default="{ row }">
              <el-switch
                :model-value="row.is_enabled"
                @change="(v: string | number | boolean) => toggleEnabled(row, v)"
              />
            </template>
          </el-table-column>
          <el-table-column label="默认" width="70">
            <template #default="{ row }">
              <el-tag v-if="row.is_default" type="danger">默认</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="360" fixed="right">
            <template #default="{ row }">
              <el-button
                link
                type="success"
                size="small"
                :loading="testingId === row.id"
                @click="testProfile(row)"
              >
                测试连通
              </el-button>
              <el-button
                v-if="!row.is_default"
                link
                type="primary"
                size="small"
                @click="setDefault(row)"
              >
                设为默认
              </el-button>
              <el-button v-else link type="info" size="small" @click="clearDefault(row)">
                取消默认
              </el-button>
              <el-button link type="primary" size="small" @click="openKeyDialog(row)">
                配置密钥
              </el-button>
              <el-button v-if="row.has_api_key" link type="warning" size="small" @click="clearApiKey(row)">
                清除密钥
              </el-button>
              <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
              <el-button link type="danger" size="small" @click="removeProfile(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="厂商预设（代码内置）" name="presets">
        <el-table v-loading="presetLoading" :data="presets" border>
          <el-table-column prop="display_name" label="厂商" width="160" />
          <el-table-column prop="key" label="键" width="140" />
          <el-table-column prop="base_url" label="默认基址" min-width="220" />
          <el-table-column prop="embedding_path" label="Embedding 路径" min-width="160" />
          <el-table-column label="Embedding" width="100">
            <template #default="{ row }">
              <el-tag :type="row.supports_embedding ? 'success' : 'info'">
                {{ row.supports_embedding ? '支持' : '不支持' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="常见模型" min-width="200">
            <template #default="{ row }">{{ (row.known_models ?? []).join('、') }}</template>
          </el-table-column>
          <el-table-column prop="chat_path" label="Chat 路径" min-width="140" />
          <el-table-column label="Chat 生成" width="100">
            <template #default="{ row }">
              <el-tag :type="row.supports_chat ? 'success' : 'info'">
                {{ row.supports_chat ? '支持' : '不支持' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="常见生成模型" min-width="180">
            <template #default="{ row }">{{ (row.known_chat_models ?? []).join('、') }}</template>
          </el-table-column>
          <el-table-column prop="rerank_path" label="Rerank 路径" min-width="120" />
          <el-table-column label="Rerank" width="90">
            <template #default="{ row }">
              <el-tag :type="row.supports_rerank ? 'success' : 'info'">
                {{ row.supports_rerank ? '支持' : '不支持' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="常见重排模型" min-width="170">
            <template #default="{ row }">
              {{ (row.known_rerank_models ?? []).join('、') || '—' }}
            </template>
          </el-table-column>
          <el-table-column prop="ocr_path" label="OCR 视觉路径" min-width="140" />
          <el-table-column prop="ocr_mode" label="OCR 模式" width="110">
            <template #default="{ row }">{{ ocrModeLabel(row.ocr_mode) }}</template>
          </el-table-column>
          <el-table-column label="OCR" width="80">
            <template #default="{ row }">
              <el-tag :type="row.supports_ocr ? 'success' : 'info'">
                {{ row.supports_ocr ? '支持' : '不支持' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="常见 OCR 模型" min-width="200">
            <template #default="{ row }">
              {{ (row.known_ocr_models ?? []).join('、') || '—' }}
            </template>
          </el-table-column>
          <el-table-column prop="note" label="说明" min-width="180" />
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="计费单价" name="prices">
        <p class="usage-head">
          按模型名配置输入/输出 token 单价（元 / 百万 tokens），仪表盘据此核算问答成本；
          只统计生成阶段，FAQ 直出、未记录 token 的记录以及 embedding / OCR 调用不计费。
        </p>
        <div class="toolbar price-toolbar">
          <el-button type="primary" @click="openPriceCreate()">新增单价</el-button>
          <div v-if="unpricedChatModels.length" class="price-suggest">
            <span class="price-suggest-label">当前生成模型未定价：</span>
            <el-button
              v-for="item in unpricedChatModels"
              :key="item.model_name"
              size="small"
              @click="openPriceCreate(item.model_name, item.provider)"
            >
              {{ item.model_name }}
            </el-button>
          </div>
        </div>
        <el-table v-loading="priceLoading" :data="prices" border>
          <el-table-column prop="model_name" label="模型名" min-width="200" />
          <el-table-column label="供应商" width="140">
            <template #default="{ row }">{{ row.provider || '—' }}</template>
          </el-table-column>
          <el-table-column label="输入单价（元/百万）" width="180" align="right">
            <template #default="{ row }">{{ formatPrice(row.input_price_per_million) }}</template>
          </el-table-column>
          <el-table-column label="输出单价（元/百万）" width="180" align="right">
            <template #default="{ row }">{{ formatPrice(row.output_price_per_million) }}</template>
          </el-table-column>
          <el-table-column label="更新时间" width="170">
            <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="130">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openPriceEdit(row)">编辑</el-button>
              <el-button link type="danger" size="small" @click="deletePrice(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!priceLoading && !prices.length" class="usage-empty">
          尚未配置任何模型单价，仪表盘成本核算会显示为 0。
        </div>
      </el-tab-pane>
    </el-tabs>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑模型档案' : '新建模型档案'"
      width="560px"
    >
      <el-form label-width="110px">
        <el-alert
          v-if="promoteDefault"
          type="info"
          :closable="false"
          show-icon
          title="保存后会设为该类型的默认模型，替换当前生效的环境配置。"
          class="tip"
        />
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="例如 aliyun-dashscope-embedding-v4" />
        </el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="form.kind" :disabled="Boolean(editingId)" @change="switchKind">
            <el-radio-button value="embedding">Embedding</el-radio-button>
            <el-radio-button value="rerank">Rerank</el-radio-button>
            <el-radio-button value="chat">Chat 生成</el-radio-button>
            <el-radio-button value="ocr">OCR</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="服务商" required>
          <el-select
            v-model="form.provider"
            filterable
            allow-create
            default-first-option
            style="width: 100%"
            @change="applyPreset"
          >
            <el-option
              v-for="option in providerOptions"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="模型名" required>
          <el-input v-model="form.model_name" :placeholder="modelNamePlaceholder" />
        </el-form-item>
        <el-form-item v-if="form.kind === 'embedding'" label="向量维度" required>
          <el-input-number v-model="form.dimensions" :min="1" style="width: 100%" />
        </el-form-item>
        <el-form-item label="API 基址" required>
          <el-input v-model="form.base_url" placeholder="https://..." />
        </el-form-item>
        <el-form-item :label="pathLabel">
          <el-input v-model="form.path" />
        </el-form-item>
        <el-form-item v-if="form.kind === 'ocr'" label="OCR 模式">
          <el-select v-model="form.ocr_mode" style="width: 100%" @change="onOcrModeChange">
            <el-option
              v-for="option in OCR_MODE_OPTIONS"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
          <div class="form-hint">
            自动按模型名判断；PaddleOCR 同步接口使用基址下的 /ocr/paddleocr；
            百度异步任务使用 AIP（https://aip.baidubce.com），适合长文档与跨页表格。
          </div>
        </el-form-item>
        <el-form-item v-if="form.kind === 'embedding'" label="批量大小">
          <el-input-number v-model="form.batch_size" :min="1" style="width: 100%" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            :placeholder="
              form.kind === 'ocr' && form.ocr_mode === 'baidu_async'
                ? editingId && editingProfileHasKey
                  ? '已配置，留空保持不变'
                  : '填写 API Key:Secret Key'
                : editingId
                  ? editingProfileHasKey
                    ? '已配置，留空保持不变'
                    : '填写供应商 API Key'
                  : '填写供应商 API Key（可稍后配置）'
            "
          />
          <div class="form-hint">
            密钥加密保存，保存后不会回显；也可在列表中单独配置。
            <template v-if="form.kind === 'ocr' && form.ocr_mode === 'baidu_async'">
              百度异步 OCR 使用文字识别控制台「应用列表」中的 API Key 与 Secret Key，用英文冒号连接，例如 API Key:Secret Key；不是 IAM 安全认证里的 Access Key。
            </template>
          </div>
        </el-form-item>
        <el-form-item label="环境密钥引用">
          <el-select
            v-model="form.secret_ref"
            clearable
            filterable
            allow-create
            default-first-option
            placeholder="库内 API Key 优先，此处可留空"
            style="width: 100%"
          >
            <el-option
              v-for="option in secretRefOptions"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.is_enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitProfile">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="keyDialogVisible"
      :title="`配置 API Key — ${keyProfile?.name ?? ''}`"
      width="480px"
    >
      <p class="key-hint">
        密钥加密后写入数据库，任何接口都不会回读明文；重复写入即覆盖，丢失后只能重新写入。
      </p>
      <el-input
        v-model="apiKey"
        type="password"
        show-password
        placeholder="供应商 API Key"
        @keyup.enter="submitApiKey"
      />
      <template #footer>
        <el-button @click="keyDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="keySubmitting" @click="submitApiKey">保存</el-button>
      </template>
    </el-dialog>
    <el-dialog
      v-model="priceDialogVisible"
      :title="priceEditingName ? '编辑计费单价' : '新增计费单价'"
      width="480px"
    >
      <el-form label-width="96px">
        <el-form-item label="模型名" required>
          <el-input
            v-model="priceForm.model_name"
            :disabled="Boolean(priceEditingName)"
            placeholder="与问答记录中的生成模型名一致"
          />
        </el-form-item>
        <el-form-item label="供应商">
          <el-input v-model="priceForm.provider" placeholder="仅用于展示，可留空" />
        </el-form-item>
        <el-form-item label="输入单价" required>
          <el-input v-model="priceForm.input_price" placeholder="元/百万 tokens，如 0.15" />
        </el-form-item>
        <el-form-item label="输出单价" required>
          <el-input v-model="priceForm.output_price" placeholder="元/百万 tokens，如 0.6" />
        </el-form-item>
      </el-form>
      <p class="key-hint">
        单价按当前配置实时核算：调整后历史问答的金额会一起变化。模型名是核算主键，改名请新增后删除旧条目。
      </p>
      <template #footer>
        <el-button @click="priceDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="priceSubmitting" @click="submitPrice">保存</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.header-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--el-color-primary);
}
.tip {
  margin-bottom: 12px;
}
.toolbar {
  margin-bottom: 12px;
}
/* 新增按钮与未定价模型快捷入口保持单行，窄屏时让快捷项自行换行。 */
.price-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.price-suggest {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.price-suggest-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.usage-head {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.usage-list {
  min-height: 120px;
}
.usage-row {
  display: grid;
  grid-template-columns: 200px minmax(0, 1fr);
  gap: 24px;
  padding: 16px 4px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.usage-row:first-child {
  border-top: none;
  padding-top: 4px;
}
.usage-slot-title {
  display: flex;
  align-items: center;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.usage-slot-title::before {
  content: '';
  width: 6px;
  height: 6px;
  margin-right: 8px;
  border-radius: 2px;
  background: var(--el-color-primary);
}
.usage-slot-desc {
  margin-top: 6px;
  padding-left: 14px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
.usage-models {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.usage-model {
  padding: 10px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-very-light);
}
.usage-model.is-inactive {
  opacity: 0.62;
}
.usage-model-main {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.usage-model-actions {
  display: inline-flex;
  margin-left: auto;
}
.usage-model-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.usage-model-meta {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.usage-model-id {
  font-family: var(--el-font-family-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  color: var(--el-text-color-regular);
}
.meta-dot {
  margin: 0 6px;
  color: var(--el-text-color-placeholder);
}
.usage-model-scope {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-regular);
}
.scope-label {
  margin-right: 6px;
  color: var(--el-text-color-secondary);
}
.usage-empty {
  padding: 10px 0;
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}
.kind-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}
.kind-usage {
  font-size: 12px;
  line-height: 1.4;
  color: var(--el-text-color-secondary);
}
@media (max-width: 900px) {
  .usage-row {
    grid-template-columns: minmax(0, 1fr);
    gap: 10px;
  }
}
.key-hint {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.form-hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}
</style>
