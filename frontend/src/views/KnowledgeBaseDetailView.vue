<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { TableInstance } from 'element-plus'
import { client, errorMessage, errorText } from '../api/client'
import { canWrite, isFullAdmin } from '../api/session'
import FaqPanel from '../components/FaqPanel.vue'
import DocumentPreviewDrawer from '../components/DocumentPreviewDrawer.vue'
import DataPanel from '../components/ui/DataPanel.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import PageHeader from '../components/ui/PageHeader.vue'
import StatusPill from '../components/ui/StatusPill.vue'
import { resolveInlineImages } from '../utils/assets'
import { formatBeijingDateTime } from '../utils/datetime'
import { escapeHtml, renderMarkdown } from '../utils/markdown'

interface KbDetail {
  id: string
  company_id: string | null
  vector_library_id: string | null
  name: string
  description: string | null
  embedding_model_id: string
  rerank_model_id: string | null
  chunk_parent_chars: number | null
  chunk_child_chars: number | null
  chunk_child_overlap_chars: number | null
  status: string
  created_at: string
  updated_at: string
}

interface Folder {
  id: string
  knowledge_base_id: string
  parent_id: string | null
  name: string
  created_at: string
  updated_at: string
}

interface DocumentItem {
  id: string
  company_id: string | null
  knowledge_base_id: string
  folder_id: string | null
  document_key: string
  filename: string
  latest_version_number: number
  status: string
  error_message: string | null
  product_names: string[]
  model_names: string[]
  created_at: string
  updated_at: string
}

interface DocumentVersion {
  id: string
  document_id: string
  version_number: number
  filename: string
  media_type: string | null
  size_bytes: number
  status: string
  is_current: boolean
  error_message: string | null
  created_at: string
  updated_at: string
}

interface IngestionJob {
  id: string
  document_id: string
  document_version_id: string
  status: string
  stage: string | null
  attempts: number
  error_details: { code?: string; message?: string; retryable?: boolean }
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

interface ChunkChild {
  ordinal: number
  content: string
  char_count: number
  rendered?: string
}

interface ChunkParent {
  ordinal: number
  content: string
  char_count: number
  page_number: number | null
  section: string | null
  block_type: string
  children: ChunkChild[]
  rendered?: string
}

interface ChunkPreviewData {
  total_parents: number
  total_children: number
  total_chars: number
  offset: number
  limit: number
  parents: ChunkParent[]
}

interface TreeNode {
  id: string
  label: string
  children: TreeNode[]
}

interface ModelProfile {
  id: string
  name: string
  kind: 'embedding' | 'rerank'
  model_name: string
  is_enabled: boolean
  is_default: boolean
}

interface ChunkDefaults {
  parent_chunk_chars: number
  child_chunk_chars: number
  child_chunk_overlap_chars: number
}

const STATUS_MAP: Record<string, { label: string; type: 'success' | 'warning' | 'danger' | 'info' | 'primary' }> = {
  completed: { label: '已完成', type: 'success' },
  queued: { label: '排队中', type: 'warning' },
  running: { label: '解析中', type: 'primary' },
  processing: { label: '解析中', type: 'primary' },
  failed: { label: '失败', type: 'danger' },
  deleting: { label: '删除中', type: 'info' },
}

const route = useRoute()
const router = useRouter()
const kbId = route.params.id as string

const kb = ref<KbDetail | null>(null)
const folders = ref<Folder[]>([])
const documents = ref<DocumentItem[]>([])
const loading = ref(false)
const currentFolderId = ref('')
const activeTab = ref('files')
const profiles = ref<ModelProfile[]>([])
const chunkDefaults = ref<ChunkDefaults | null>(null)

function profileName(id: string | null): string {
  if (!id) return '未启用'
  return profiles.value.find((p) => p.id === id)?.name ?? '未知模型'
}

// —— 配置表单：名称/描述/分块参数可改，模型只读。留空表示跟随全局默认。——

const configForm = reactive({
  name: '',
  description: '',
  parentChars: null as number | null,
  childChars: null as number | null,
  overlapChars: null as number | null,
  saving: false,
})

function initConfigForm() {
  if (!kb.value) return
  configForm.name = kb.value.name
  configForm.description = kb.value.description ?? ''
  configForm.parentChars = kb.value.chunk_parent_chars
  configForm.childChars = kb.value.chunk_child_chars
  configForm.overlapChars = kb.value.chunk_child_overlap_chars
}

const chunkDefaultsHint = computed(() => {
  const defaults = chunkDefaults.value
  if (!defaults) return ''
  return `留空跟随全局默认：父块 ${defaults.parent_chunk_chars} · 子块 ${defaults.child_chunk_chars} · 重叠 ${defaults.child_chunk_overlap_chars}`
})

async function saveConfig() {
  if (!kb.value || !configForm.name.trim()) {
    ElMessage.warning('请填写知识库名称')
    return
  }
  configForm.saving = true
  try {
    const { data } = await client.patch<KbDetail>(`/knowledge-bases/${kbId}`, {
      name: configForm.name.trim(),
      // 描述留空即清空；分块参数传 null 表示跟随全局默认。
      description: configForm.description.trim() || null,
      chunk_parent_chars: configForm.parentChars,
      chunk_child_chars: configForm.childChars,
      chunk_child_overlap_chars: configForm.overlapChars,
    })
    kb.value = data
    ElMessage.success('配置已保存')
  } catch (error) {
    errorMessage(error, '保存配置失败')
  } finally {
    configForm.saving = false
  }
}

// 文档列表走服务端分页：有搜索词时跨目录搜索，否则只看当前目录（根目录用 root_only）。
const documentTotal = ref(0)
const documentPage = ref(1)
const documentPageSize = 20
const documentSearch = ref('')

const tree = computed<TreeNode[]>(() => {
  const make = (parentId: string | null): TreeNode[] =>
    folders.value
      .filter((folder) => folder.parent_id === parentId)
      .sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
      .map((folder) => ({ id: folder.id, label: folder.name, children: make(folder.id) }))
  return [{ id: '', label: '根目录', children: make(null) }]
})

function statusOf(document: DocumentItem) {
  return STATUS_MAP[document.status] ?? { label: document.status, type: 'info' as const }
}

function formatDateTime(value: string): string {
  // 库存 UTC，界面按北京时间展示。
  return formatBeijingDateTime(value)
}

async function loadFolders() {
  const { data } = await client.get<Folder[]>(`/knowledge-bases/${kbId}/folders`)
  folders.value = data
}

async function loadDocuments() {
  const params: Record<string, string | number | boolean> = {
    offset: (documentPage.value - 1) * documentPageSize,
    limit: documentPageSize,
  }
  const keyword = documentSearch.value.trim()
  if (keyword) {
    params.q = keyword
  } else if (currentFolderId.value) {
    params.folder_id = currentFolderId.value
  } else {
    params.root_only = true
  }
  const { data, headers } = await client.get<DocumentItem[]>(
    `/knowledge-bases/${kbId}/documents`,
    { params },
  )
  documents.value = data
  const total = Number(headers['x-total-count'])
  documentTotal.value = Number.isFinite(total) ? total : data.length
  // 删除末页最后一条后回退一页，避免停留在空页。
  if (data.length === 0 && documentPage.value > 1) {
    documentPage.value -= 1
    await loadDocuments()
    return
  }
  scheduleRefresh()
}

async function changeDocumentPage() {
  try {
    await loadDocuments()
  } catch (error) {
    errorMessage(error, '加载文件列表失败')
  }
}

function applyDocumentSearch() {
  documentPage.value = 1
  void changeDocumentPage()
}

function onDocumentPageChange(page: number) {
  documentPage.value = page
  void changeDocumentPage()
}

// 搜索是跨目录的，切换目录时清空搜索并回到第一页，避免目录筛选看起来失效。
watch(currentFolderId, () => {
  documentSearch.value = ''
  documentPage.value = 1
  void changeDocumentPage()
})

async function reloadFiles() {
  loading.value = true
  try {
    await Promise.all([loadFolders(), loadDocuments()])
  } catch (error) {
    errorMessage(error, '加载文件列表失败')
  } finally {
    loading.value = false
  }
}

// 有文档在排队或解析中时定时刷新，入库完成后自动停止。
let refreshTimer: number | undefined
function scheduleRefresh() {
  const pending = documents.value.some(
    (d) => d.status === 'queued' || d.status === 'running' || d.status === 'processing',
  )
  if (!pending) {
    if (refreshTimer !== undefined) {
      window.clearInterval(refreshTimer)
      refreshTimer = undefined
    }
    return
  }
  if (refreshTimer === undefined) {
    refreshTimer = window.setInterval(() => {
      void loadDocuments().catch(() => undefined)
    }, 6000)
  }
}
onBeforeUnmount(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer)
})

// —— 失败文档：入库详情与重试 ——

const STAGE_MAP: Record<string, string> = {
  load: '读取文件',
  parse: '解析内容',
  chunk: '切块',
  embed: '向量化',
  index: '写入索引',
  complete: '完成',
}

function stageLabel(stage: string | null): string {
  if (!stage) return '未开始'
  return STAGE_MAP[stage] ?? stage
}

const JOB_STATUS_MAP: Record<string, string> = {
  queued: '排队中',
  running: '处理中',
  processing: '处理中',
  completed: '已完成',
  failed: '失败',
}

function statusLabel(status: string): string {
  return JOB_STATUS_MAP[status] ?? status
}

const ingestionDialog = reactive({
  visible: false,
  loading: false,
  document: null as DocumentItem | null,
  job: null as IngestionJob | null,
})

async function openIngestionDetail(document: DocumentItem) {
  ingestionDialog.document = document
  ingestionDialog.job = null
  ingestionDialog.visible = true
  ingestionDialog.loading = true
  try {
    const { data } = await client.get<IngestionJob>(`/documents/${document.id}/ingestion`)
    ingestionDialog.job = data
  } catch (error) {
    errorMessage(error, '加载入库详情失败')
  } finally {
    ingestionDialog.loading = false
  }
}

const retryingId = ref('')

async function retryDocument(document: DocumentItem) {
  retryingId.value = document.id
  try {
    await client.post<IngestionJob>(`/documents/${document.id}/retry`)
    ElMessage.success('已重新排队，正在后台解析')
    ingestionDialog.visible = false
    // 状态回到 queued，loadDocuments 会重新启动轮询。
    await loadDocuments()
  } catch (error) {
    errorMessage(error, '重试失败')
  } finally {
    retryingId.value = ''
  }
}

// —— 目录操作 ——

function validateFolderName(value: string): boolean {
  const trimmed = value.trim()
  return trimmed.length >= 1 && trimmed.length <= 160
}

async function createFolder(parentId: string | null) {
  let name: string
  try {
    const result = await ElMessageBox.prompt('目录名称（同一级内不可重名）', '新建目录', {
      inputValidator: (value) => validateFolderName(value) || '名称需为 1-160 个字符',
      confirmButtonText: '创建',
      cancelButtonText: '取消',
    })
    name = result.value.trim()
  } catch {
    return
  }
  try {
    await client.post(`/knowledge-bases/${kbId}/folders`, { name, parent_id: parentId })
    ElMessage.success('目录已创建')
    await reloadFiles()
  } catch (error) {
    errorMessage(error, '创建目录失败')
  }
}

async function renameFolder(node: TreeNode) {
  let name: string
  try {
    const result = await ElMessageBox.prompt('新的目录名称', '重命名目录', {
      inputValue: node.label,
      inputValidator: (value) => validateFolderName(value) || '名称需为 1-160 个字符',
      confirmButtonText: '保存',
      cancelButtonText: '取消',
    })
    name = result.value.trim()
  } catch {
    return
  }
  try {
    await client.patch(`/knowledge-bases/${kbId}/folders/${node.id}`, { name })
    ElMessage.success('已重命名')
    await loadFolders()
  } catch (error) {
    errorMessage(error, '重命名失败')
  }
}

async function removeFolder(node: TreeNode) {
  try {
    await ElMessageBox.confirm(
      `删除目录「${node.label}」？目录必须为空，文件不会受影响。`,
      '删除目录',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/knowledge-bases/${kbId}/folders/${node.id}`)
    ElMessage.success('目录已删除')
    // 目录删除后其中的文件落回根目录；若正在浏览该目录，先回到根目录。
    if (currentFolderId.value === node.id) currentFolderId.value = ''
    await reloadFiles()
  } catch (error) {
    errorMessage(error, '删除目录失败')
  }
}

function onFolderCommand(command: string, node: TreeNode) {
  if (command === 'new') void createFolder(node.id || null)
  else if (command === 'rename') void renameFolder(node)
  else if (command === 'delete') void removeFolder(node)
}

// —— 上传 ——

interface CatalogEntry {
  id: string
  product_name: string
  model_name: string
  created_at: string
}

// 上传标签从全局产品目录选择，与知识库无关；顺手新增写入全局目录。
const catalogEntries = ref<CatalogEntry[]>([])

const productNames = computed(() => {
  const names = new Set(catalogEntries.value.map((entry) => entry.product_name))
  return [...names].sort((a, b) => a.localeCompare(b, 'zh'))
})

// 型号选项跟随已选产品名称联动：未选产品时展示全部，选中后只展示对应型号。
function modelOptionsFor(products: string[]): string[] {
  const selected = new Set(products)
  const names = new Set(
    catalogEntries.value
      .filter((entry) => selected.size === 0 || selected.has(entry.product_name))
      .map((entry) => entry.model_name),
  )
  return [...names].sort((a, b) => a.localeCompare(b, 'zh'))
}

// 换产品后剔除不再属于所选产品的型号，避免提交时被目录校验拒绝。
function pruneModels(products: string[], models: string[]): string[] {
  const selected = new Set(products)
  if (selected.size === 0) return models
  const valid = new Set(
    catalogEntries.value
      .filter((entry) => selected.has(entry.product_name))
      .map((entry) => entry.model_name),
  )
  return models.filter((name) => valid.has(name))
}

const modelNames = computed(() => modelOptionsFor(uploadDialog.productNames))

async function loadCatalogOptions() {
  const { data } = await client.get<CatalogEntry[]>('/product-catalog')
  catalogEntries.value = data
}

const quickAdd = reactive({
  open: false,
  productName: '',
  modelName: '',
  submitting: false,
})

function toggleQuickAdd() {
  quickAdd.open = !quickAdd.open
  quickAdd.productName = ''
  quickAdd.modelName = ''
}

async function submitQuickAdd() {
  const productName = quickAdd.productName.trim()
  const modelName = quickAdd.modelName.trim()
  if (!productName || !modelName) {
    ElMessage.warning('请填写产品名称和产品型号')
    return
  }
  quickAdd.submitting = true
  try {
    await client.post('/product-catalog', { product_name: productName, model_name: modelName })
    ElMessage.success('已登记到产品目录')
    quickAdd.open = false
    // 新登记的标签自动选中，省去再回下拉框查找。
    if (!uploadDialog.productNames.includes(productName)) {
      uploadDialog.productNames.push(productName)
    }
    if (!uploadDialog.modelNames.includes(modelName)) {
      uploadDialog.modelNames.push(modelName)
    }
    await loadCatalogOptions()
  } catch (error) {
    errorMessage(error, '登记产品失败')
  } finally {
    quickAdd.submitting = false
  }
}

interface UploadItem {
  key: number
  file: File
  // 入库文件名，可在列表中手动修改；不写扩展名时沿用原文件扩展名。
  name: string
  state: 'pending' | 'uploading' | 'done' | 'failed'
  error: string
  // 浏览器上传字节进度（0-100），完成后置 100。
  progress: number
}

let uploadItemSeq = 0

const uploadDialog = reactive({
  visible: false,
  submitting: false,
  mode: 'batch' as 'batch' | 'replace',
  replaceTarget: null as DocumentItem | null,
  items: [] as UploadItem[],
  productNames: [] as string[],
  modelNames: [] as string[],
  general: false,
})

function openUploadDialog() {
  uploadDialog.mode = 'batch'
  uploadDialog.replaceTarget = null
  uploadDialog.items = []
  uploadDialog.productNames = []
  uploadDialog.modelNames = []
  uploadDialog.general = false
  quickAdd.open = false
  uploadDialog.visible = true
  // 打开时顺带刷新选项，拿到其他入口刚写入的新产品标签。
  void loadCatalogOptions().catch(() => undefined)
}

function openReplaceDialog(document: DocumentItem) {
  uploadDialog.mode = 'replace'
  uploadDialog.replaceTarget = document
  uploadDialog.items = []
  // 预填现有标签，避免重传时错标；无标签文档视为通用资料。
  uploadDialog.productNames = [...document.product_names]
  uploadDialog.modelNames = [...document.model_names]
  uploadDialog.general =
    document.product_names.length === 0 && document.model_names.length === 0
  quickAdd.open = false
  uploadDialog.visible = true
  void loadCatalogOptions().catch(() => undefined)
}

watch(
  () => uploadDialog.productNames,
  (products) => {
    uploadDialog.modelNames = pruneModels(products, uploadDialog.modelNames)
  },
)

const uploadInputRef = ref<HTMLInputElement | null>(null)

function pickFile() {
  uploadInputRef.value?.click()
}

function onUploadFileChange(event: Event) {
  const target = event.target as HTMLInputElement
  const selected = Array.from(target.files ?? [])
  // 清空 input，保证重复选择同一文件也能再次触发 change。
  target.value = ''
  if (selected.length === 0) return
  if (uploadDialog.mode === 'replace') {
    uploadDialog.items = [
      {
        key: ++uploadItemSeq,
        file: selected[0],
        name: selected[0].name,
        state: 'pending',
        error: '',
        progress: 0,
      },
    ]
    return
  }
  for (const file of selected) {
    uploadDialog.items.push({
      key: ++uploadItemSeq,
      file,
      name: file.name,
      state: 'pending',
      error: '',
      progress: 0,
    })
  }
}

function removeUploadItem(item: UploadItem) {
  uploadDialog.items = uploadDialog.items.filter((entry) => entry !== item)
}

function retryUploadItem(item: UploadItem) {
  item.state = 'pending'
  item.error = ''
  item.progress = 0
}

// 入库文件名：手动改过的名字直接使用；没写扩展名时沿用原文件扩展名。
function storedName(item: UploadItem): string {
  const name = item.name.trim()
  if (!name) return ''
  if (!name.includes('.')) {
    const dot = item.file.name.lastIndexOf('.')
    if (dot > 0) return name + item.file.name.slice(dot)
  }
  return name
}

// 批量模式以文件名为文档键：与库内已有文档同名时提示将更新为新版本。
function updatingTarget(name: string): string | null {
  if (uploadDialog.mode !== 'batch') return null
  const key = name.trim()
  if (!key) return null
  const document = documents.value.find((item) => item.document_key === key)
  return document ? document.filename : null
}

async function uploadOne(
  item: UploadItem,
  productNames: string[],
  modelNames: string[],
) {
  item.state = 'uploading'
  item.error = ''
  item.progress = 0
  try {
    const name = storedName(item)
    await client.post(`/knowledge-bases/${kbId}/documents`, item.file, {
      params: {
        company_id: kb.value?.company_id ?? '',
        filename: name,
        // 批量上传以文件名为稳定文档标识：同名即覆盖为新版本；
        // 替换模式固定为目标文档键，新文件名可与原文件不同。
        document_key:
          uploadDialog.mode === 'replace' && uploadDialog.replaceTarget
            ? uploadDialog.replaceTarget.document_key
            : name,
        ...(uploadDialog.general ? {} : { product_names: productNames, model_names: modelNames }),
        ...(currentFolderId.value ? { folder_id: currentFolderId.value } : {}),
      },
      // 列表参数序列化为重复键（product_names=a&product_names=b），FastAPI 才能解析成数组。
      paramsSerializer: { indexes: null },
      headers: { 'Content-Type': item.file.type || 'application/octet-stream' },
      onUploadProgress: (event) => {
        // 字节传完后服务端还要落盘登记，先停在 99%，等请求返回再标完成。
        if (!event.total) return
        item.progress = Math.min(99, Math.round((event.loaded / event.total) * 100))
      },
    })
    item.progress = 100
    item.state = 'done'
  } catch (error) {
    item.state = 'failed'
    item.error = errorText(error, '上传失败')
  }
}

async function submitUpload() {
  const pending = uploadDialog.items.filter((item) => item.state !== 'done')
  if (pending.length === 0) {
    ElMessage.warning('请先选择文件')
    return
  }
  const productNames = uploadDialog.productNames.map((value) => value.trim()).filter(Boolean)
  const modelNames = uploadDialog.modelNames.map((value) => value.trim()).filter(Boolean)
  // 通用资料不绑定产品，不写产品元数据；否则产品名称与型号必选。
  if (!uploadDialog.general && productNames.length === 0) {
    ElMessage.warning('请至少选择一个产品名称，或勾选「通用资料」')
    return
  }
  if (!uploadDialog.general && modelNames.length === 0) {
    ElMessage.warning('请至少选择一个产品型号，或勾选「通用资料」')
    return
  }
  // 入库文件名必填：空白名无法通过后端文件名校验，先在前端拦下。
  const unnamed = pending.find((item) => !storedName(item))
  if (unnamed) {
    ElMessage.warning(`存在文件名为空的条目（原文件「${unnamed.file.name}」），请填写文件名`)
    return
  }
  // 批次内同名文件会先后写入同一文档键，后传的覆盖先传的，直接阻止并提示。
  if (uploadDialog.mode === 'batch') {
    const seen = new Set<string>()
    const duplicated = uploadDialog.items.find((item) => {
      const name = storedName(item)
      if (seen.has(name)) return true
      seen.add(name)
      return false
    })
    if (duplicated) {
      ElMessage.warning(
        `批次内存在同名文件「${storedName(duplicated)}」，会互相覆盖为新版本，请先修改文件名`,
      )
      return
    }
  }
  uploadDialog.submitting = true
  try {
    // 逐个顺序上传，单个失败不影响后续文件。
    for (const item of pending) {
      await uploadOne(item, productNames, modelNames)
    }
    const failed = uploadDialog.items.filter((item) => item.state === 'failed')
    const succeeded = uploadDialog.items.length - failed.length
    if (failed.length === 0) {
      ElMessage.success(
        uploadDialog.mode === 'replace'
          ? `「${uploadDialog.replaceTarget?.filename}」新版本已上传，正在后台解析`
          : `已上传 ${succeeded} 个文件，正在后台解析`,
      )
      uploadDialog.visible = false
    } else {
      ElMessage.warning(`上传完成：成功 ${succeeded} 个，失败 ${failed.length} 个，失败项可单独重试`)
    }
    await reloadFiles()
  } finally {
    uploadDialog.submitting = false
  }
}

// —— 修改产品标签（入库后覆盖写入，单个与批量共用） ——

const labelDialog = reactive({
  visible: false,
  submitting: false,
  targets: [] as DocumentItem[],
  productNames: [] as string[],
  modelNames: [] as string[],
  general: false,
})

const labelModelNames = computed(() => modelOptionsFor(labelDialog.productNames))

function openLabelDialog(targets: DocumentItem[]) {
  if (targets.length === 0) return
  labelDialog.targets = targets
  // 以第一个文件为起点预填；批量时统一覆盖成这组标签，弹窗内会明示影响范围。
  const first = targets[0]
  labelDialog.productNames = [...first.product_names]
  labelDialog.modelNames = [...first.model_names]
  labelDialog.general =
    first.product_names.length === 0 && first.model_names.length === 0
  labelDialog.visible = true
  void loadCatalogOptions().catch(() => undefined)
}

watch(
  () => labelDialog.productNames,
  (products) => {
    labelDialog.modelNames = pruneModels(products, labelDialog.modelNames)
  },
)

async function submitLabelUpdate() {
  const productNames = labelDialog.productNames.map((value) => value.trim()).filter(Boolean)
  const modelNames = labelDialog.modelNames.map((value) => value.trim()).filter(Boolean)
  if (!labelDialog.general && productNames.length === 0) {
    ElMessage.warning('请选择产品名称，或勾选「通用资料」清空标签')
    return
  }
  if (!labelDialog.general && modelNames.length === 0) {
    ElMessage.warning('请选择产品型号，或勾选「通用资料」清空标签')
    return
  }
  const payload = {
    product_names: labelDialog.general ? [] : productNames,
    model_names: labelDialog.general ? [] : modelNames,
  }
  const count = labelDialog.targets.length
  labelDialog.submitting = true
  try {
    if (count === 1) {
      await client.patch(`/documents/${labelDialog.targets[0].id}/product-labels`, payload)
    } else {
      await client.post('/documents/product-labels/batch', {
        document_ids: labelDialog.targets.map((document) => document.id),
        ...payload,
      })
    }
    ElMessage.success(count > 1 ? `已更新 ${count} 个文件的标签` : '产品标签已更新')
    labelDialog.visible = false
    documentTableRef.value?.clearSelection()
    await loadDocuments()
  } catch (error) {
    errorMessage(error, '更新产品标签失败')
  } finally {
    labelDialog.submitting = false
  }
}

// —— 移动文档 ——

const moveDialog = reactive({
  visible: false,
  submitting: false,
  document: null as DocumentItem | null,
  targetId: '',
})

function openMoveDialog(document: DocumentItem) {
  moveDialog.document = document
  moveDialog.targetId = document.folder_id ?? ''
  moveDialog.visible = true
}

async function submitMove() {
  const document = moveDialog.document
  if (!document || moveDialog.targetId === (document.folder_id ?? '')) {
    moveDialog.visible = false
    return
  }
  moveDialog.submitting = true
  try {
    await client.patch(`/documents/${document.id}`, {
      folder_id: moveDialog.targetId === '' ? null : moveDialog.targetId,
    })
    ElMessage.success('已移动')
    moveDialog.visible = false
    await loadDocuments()
  } catch (error) {
    errorMessage(error, '移动失败')
  } finally {
    moveDialog.submitting = false
  }
}

// —— 原文件预览 / 下载 ——

// 用 axios 取文件二进制再交给浏览器，保证携带管理员 Token 鉴权头。
async function fetchFileBlob(
  item: { id: string },
  disposition: 'inline' | 'attachment' = 'attachment',
): Promise<Blob> {
  const { data } = await client.get<Blob>(`/documents/${item.id}/file`, {
    params: { disposition },
    responseType: 'blob',
  })
  return data
}

// 多格式预览交给独立抽屉组件：PDF/图片/文本/Office/表格/PPT 各有渲染分支。
const previewVisible = ref(false)
const previewItem = ref<DocumentItem | null>(null)

function previewFile(item: DocumentItem) {
  previewItem.value = item
  previewVisible.value = true
}

async function downloadFile(item: { id: string; filename: string }) {
  try {
    const blob = await fetchFileBlob(item)
    const url = URL.createObjectURL(blob)
    const anchor = window.document.createElement('a')
    anchor.href = url
    anchor.download = item.filename
    window.document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(url)
    ElMessage.success('已开始下载')
  } catch (error) {
    errorMessage(error, '下载原文件失败')
  }
}

// —— 切块预览 ——

const preview = reactive({
  visible: false,
  loading: false,
  loadingMore: false,
  document: null as DocumentItem | null,
  versions: [] as DocumentVersion[],
  versionId: '',
  parents: [] as ChunkParent[],
  totalParents: 0,
  totalChildren: 0,
  totalChars: 0,
  activeNames: [] as string[],
})

async function openPreview(document: DocumentItem) {
  preview.document = document
  preview.versions = []
  preview.versionId = ''
  preview.parents = []
  preview.totalParents = 0
  preview.totalChildren = 0
  preview.totalChars = 0
  preview.activeNames = []
  preview.visible = true
  try {
    const { data } = await client.get<DocumentVersion[]>(`/documents/${document.id}/versions`)
    preview.versions = data
    preview.versionId = data.find((v) => v.is_current)?.id ?? data[0]?.id ?? ''
    if (preview.versionId) await loadChunks(true)
  } catch (error) {
    errorMessage(error, '加载版本列表失败')
  }
}

// 切块正文渲染：后端已把图片占位符换成 ![](资产URL)，这里先转成带鉴权的 blob 地址。
// 表格块按管道行格式渲染为 HTML 表格，普通块保持原文并内嵌图片。
// 注意不用 loading="lazy"：抽屉+折叠面板里原生懒加载常不触发，导致图片一直不显示。
function chunkHtml(parent: ChunkParent): string {
  const content = parent.rendered ?? parent.content
  if (parent.block_type === 'table') return renderMarkdown(content, 2, { lazyImages: false })
  return escapeHtml(content).replace(
    /!\[([^\]]*)\]\(([^)]+)\)/g,
    '<img class="md-image" src="$2" alt="$1">',
  )
}

function childChunkHtml(child: ChunkChild): string {
  const content = child.rendered ?? child.content
  return escapeHtml(content).replace(
    /!\[([^\]]*)\]\(([^)]+)\)/g,
    '<img class="md-image" src="$2" alt="$1">',
  )
}

async function renderChunkImages(parent: ChunkParent): Promise<void> {
  parent.rendered = await resolveInlineImages(parent.content)
  await Promise.all(
    parent.children.map(async (child) => {
      child.rendered = await resolveInlineImages(child.content)
    }),
  )
}

async function loadChunks(reset: boolean) {
  if (!preview.document || !preview.versionId) return
  if (reset) preview.loading = true
  else preview.loadingMore = true
  try {
    const { data } = await client.get<ChunkPreviewData>(
      `/documents/${preview.document.id}/versions/${preview.versionId}/chunks`,
      { params: { offset: reset ? 0 : preview.parents.length, limit: 100 } },
    )
    preview.totalParents = data.total_parents
    preview.totalChildren = data.total_children
    preview.totalChars = data.total_chars
    const parents = reset ? data.parents : [...preview.parents, ...data.parents]
    await Promise.all(parents.map(renderChunkImages))
    preview.parents = parents
    preview.activeNames =
      preview.parents.length > 0 ? [String(preview.parents[0].ordinal)] : []
  } catch (error) {
    errorMessage(error, '加载切块预览失败')
  } finally {
    preview.loading = false
    preview.loadingMore = false
  }
}

function formatChars(value: number): string {
  return value >= 10000 ? `${(value / 10000).toFixed(1)} 万` : String(value)
}

// 版本回滚：仅翻转后端的当前版本标志，检索/FAQ/原文件预览随之切换。
const activatingVersionId = ref('')
const selectedVersion = computed(() =>
  preview.versions.find((item) => item.id === preview.versionId),
)

async function activateVersion() {
  const document = preview.document
  const version = preview.versions.find((item) => item.id === preview.versionId)
  if (!document || !version || version.is_current) return
  try {
    await ElMessageBox.confirm(
      `把 v${version.version_number} 设为当前检索版本？检索、FAQ 匹配与原文件预览都会切换到该版本，最新上传的 v${document.latest_version_number} 仍保留。`,
      '切换当前版本',
      { type: 'warning', confirmButtonText: '切换', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  activatingVersionId.value = version.id
  try {
    await client.post(`/documents/${document.id}/versions/${version.id}/activate`)
    ElMessage.success(`已切换到 v${version.version_number}`)
    const { data } = await client.get<DocumentVersion[]>(`/documents/${document.id}/versions`)
    preview.versions = data
    preview.versionId = version.id
    await loadChunks(true)
  } catch (error) {
    errorMessage(error, '切换版本失败')
  } finally {
    activatingVersionId.value = ''
  }
}

// —— 删除文档 ——

async function removeDocument(document: DocumentItem) {
  try {
    await ElMessageBox.confirm(
      `删除文件「${document.filename}」？其全部版本、切块与向量都会被删除，且不可恢复。`,
      '删除文件',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/documents/${document.id}`)
    ElMessage.success('文件已删除')
    await loadDocuments()
  } catch (error) {
    errorMessage(error, '删除文件失败')
  }
}

// —— 批量删除 ——

const documentTableRef = ref<TableInstance>()
const selectedDocuments = ref<DocumentItem[]>([])

function onSelectionChange(rows: DocumentItem[]) {
  selectedDocuments.value = rows
}

// 切换目录时清空勾选，避免对不在当前列表中的文件误删。
watch(currentFolderId, () => {
  documentTableRef.value?.clearSelection()
})

async function batchDeleteDocuments() {
  const count = selectedDocuments.value.length
  if (count === 0) return
  try {
    await ElMessageBox.confirm(
      `删除选中的 ${count} 个文件？其全部版本、切块与向量都会被删除，且不可恢复。`,
      '批量删除文件',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.post(`/knowledge-bases/${kbId}/documents/batch-delete`, {
      document_ids: selectedDocuments.value.map((document) => document.id),
    })
    ElMessage.success(`已删除 ${count} 个文件`)
    documentTableRef.value?.clearSelection()
    await loadDocuments()
  } catch (error) {
    errorMessage(error, '批量删除失败')
  }
}

onMounted(async () => {
  try {
    // 模型档案仅管理员可读；其他角色不拉取，配置 tab 也不展示。
    const [kbRes, defaultsRes] = await Promise.all([
      client.get<KbDetail>(`/knowledge-bases/${kbId}`),
      client.get<ChunkDefaults>('/admin/chunk-defaults'),
    ])
    kb.value = kbRes.data
    chunkDefaults.value = defaultsRes.data
    if (isFullAdmin.value) {
      const profileRes = await client.get<ModelProfile[]>('/admin/model-profiles')
      profiles.value = profileRes.data
    }
    initConfigForm()
    await reloadFiles()
    void loadCatalogOptions().catch(() => undefined)
    // 全局搜索命中文档时带 doc 参数跳转，进入后直接打开原文件预览。
    const targetDoc = route.query.doc
    if (typeof targetDoc === 'string' && targetDoc) {
      try {
        const { data } = await client.get<DocumentItem>(`/documents/${targetDoc}`)
        previewFile(data)
      } catch {
        // 文档已删除或不属于当前账号可见范围时保持常规页面即可。
      }
    }
  } catch (error) {
    errorMessage(error, '加载知识库失败')
  }
})
</script>

<template>
  <div v-if="kb" class="kb-detail-page">
    <PageHeader :title="kb.name" eyebrow="知识库详情" :subtitle="kb.description || undefined">
      <template #meta>
        <StatusPill
          :label="kb.status === 'active' ? '启用中' : '已归档'"
          :tone="kb.status === 'active' ? 'success' : 'info'"
        />
        <span class="header-meta">更新于 {{ formatDateTime(kb.updated_at) }}</span>
      </template>
      <template #actions>
        <el-button size="small" @click="router.push('/kbs')">
          <el-icon><Back /></el-icon>返回列表
        </el-button>
      </template>
    </PageHeader>

    <el-tabs v-model="activeTab" class="detail-tabs">
      <el-tab-pane label="文件" name="files">
        <el-alert
          v-if="kb.status !== 'active'"
          type="warning"
          :closable="false"
          show-icon
          title="知识库已归档，文件与目录不可再变更。"
          class="tab-alert"
        />
        <div class="files-layout">
          <DataPanel class="folder-panel" title="目录" flush>
            <template #actions>
              <el-button
                v-if="canWrite && kb.status === 'active'"
                size="small"
                type="primary"
                plain
                :icon="'Plus'"
                @click="createFolder(null)"
              >
                新建目录
              </el-button>
            </template>
            <div class="tree-body">
              <el-tree
                :data="tree"
                node-key="id"
                highlight-current
                :current-node-key="currentFolderId"
                :expand-on-click-node="false"
                default-expand-all
                @node-click="(node: TreeNode) => (currentFolderId = node.id)"
              >
                <template #default="{ data }">
                  <div class="tree-node">
                    <el-icon><Folder /></el-icon>
                    <span class="node-label">{{ data.label }}</span>
                    <el-dropdown
                      v-if="canWrite && kb.status === 'active'"
                      trigger="click"
                      class="node-more"
                      @command="(command: string) => onFolderCommand(command, data)"
                    >
                      <el-icon><MoreFilled /></el-icon>
                      <template #dropdown>
                        <el-dropdown-menu>
                          <el-dropdown-item command="new">新建子目录</el-dropdown-item>
                          <el-dropdown-item v-if="data.id" command="rename">重命名</el-dropdown-item>
                          <el-dropdown-item v-if="data.id" command="delete" divided>删除</el-dropdown-item>
                        </el-dropdown-menu>
                      </template>
                    </el-dropdown>
                  </div>
                </template>
              </el-tree>
            </div>
          </DataPanel>

          <DataPanel v-loading="loading" class="document-panel" title="文件" :count="`${documentTotal} 份`" flush>
            <template #actions>
              <el-button
                v-if="canWrite && kb.status === 'active' && selectedDocuments.length > 0"
                size="small"
                plain
                :icon="'EditPen'"
                @click="openLabelDialog(selectedDocuments)"
              >
                批量改标签（{{ selectedDocuments.length }}）
              </el-button>
              <el-button
                v-if="canWrite && kb.status === 'active' && selectedDocuments.length > 0"
                size="small"
                type="danger"
                plain
                :icon="'Delete'"
                @click="batchDeleteDocuments"
              >
                批量删除（{{ selectedDocuments.length }}）
              </el-button>
              <el-button
                v-if="canWrite && kb.status === 'active' && kb.company_id"
                size="small"
                type="primary"
                :icon="'Upload'"
                @click="openUploadDialog"
              >
                上传文件
              </el-button>
              <el-tooltip
                v-else-if="canWrite && kb.status === 'active'"
                content="该知识库缺少公司归属，无法上传文件"
              >
                <el-button size="small" type="primary" disabled>上传文件</el-button>
              </el-tooltip>
            </template>
            <div class="document-toolbar">
              <el-input
                v-model="documentSearch"
                size="small"
                clearable
                class="document-search"
                placeholder="搜索文件名或文档标识（全库）"
                @keyup.enter="applyDocumentSearch"
                @clear="applyDocumentSearch"
              >
                <template #prefix>
                  <el-icon><Search /></el-icon>
                </template>
              </el-input>
            </div>
            <el-table
              ref="documentTableRef"
              :data="documents"
              row-key="id"
              size="small"
              @selection-change="onSelectionChange"
            >
              <template #empty>
                <EmptyState
                  :title="documentSearch.trim() ? '没有匹配的文件' : '当前目录暂无文件'"
                  :text="
                    documentSearch.trim()
                      ? '换个关键词试试，搜索范围覆盖整个知识库。'
                      : '换个目录，或用上方搜索框在全库查找文件名。'
                  "
                />
              </template>
              <el-table-column type="selection" width="40" :selectable="() => canWrite && kb !== null && kb.status === 'active'" />
              <el-table-column prop="filename" label="文件名" min-width="200" show-overflow-tooltip />
              <el-table-column label="状态" width="120">
                <template #default="{ row }">
                  <div class="status-cell">
                    <el-tooltip
                      v-if="row.error_message"
                      :content="row.error_message"
                      placement="top"
                    >
                      <StatusPill :label="statusOf(row).label" :tone="statusOf(row).type" :dot="false" />
                    </el-tooltip>
                    <StatusPill v-else :label="statusOf(row).label" :tone="statusOf(row).type" />
                    <el-button
                      v-if="row.status === 'failed'"
                      size="small"
                      text
                      type="primary"
                      @click="openIngestionDetail(row)"
                    >
                      详情
                    </el-button>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="latest_version_number" label="版本" width="60" />
              <el-table-column label="产品名称" min-width="150">
                <template #default="{ row }">
                  <div class="label-cell">
                    <span v-if="row.product_names.length" class="label-text">{{ row.product_names.join('、') }}</span>
                    <span v-else class="label-text label-muted">未标注</span>
                    <el-button
                      v-if="canWrite && kb.status === 'active'"
                      class="label-edit"
                      size="small"
                      text
                      :icon="'Edit'"
                      aria-label="修改产品标签"
                      @click.stop="openLabelDialog([row])"
                    />
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="产品型号" min-width="160">
                <template #default="{ row }">
                  <div class="label-cell">
                    <span v-if="row.model_names.length" class="label-text">{{ row.model_names.join('、') }}</span>
                    <span v-else class="label-text label-muted">未标注</span>
                    <el-button
                      v-if="canWrite && kb.status === 'active'"
                      class="label-edit"
                      size="small"
                      text
                      :icon="'Edit'"
                      aria-label="修改产品标签"
                      @click.stop="openLabelDialog([row])"
                    />
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="更新时间" width="150">
                <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="350" fixed="right">
                <template #default="{ row }">
                  <div class="file-actions">
                    <el-button size="small" text type="primary" @click="previewFile(row)">预览</el-button>
                    <el-button size="small" text type="primary" @click="downloadFile(row)">下载</el-button>
                    <el-button size="small" text @click="openPreview(row)">切块</el-button>
                    <el-button v-if="canWrite" size="small" text @click="openMoveDialog(row)">移动</el-button>
                    <el-button
                      v-if="canWrite"
                      size="small"
                      text
                      type="primary"
                      :disabled="kb.status !== 'active'"
                      @click="openReplaceDialog(row)"
                    >
                      新版本
                    </el-button>
                    <el-button
                      v-if="canWrite && row.status === 'failed'"
                      size="small"
                      text
                      type="warning"
                      :loading="retryingId === row.id"
                      :disabled="kb.status !== 'active'"
                      @click="retryDocument(row)"
                    >
                      重试
                    </el-button>
                    <el-button v-if="canWrite" size="small" text type="danger" @click="removeDocument(row)">删除</el-button>
                  </div>
                </template>
              </el-table-column>
            </el-table>
            <template v-if="documentTotal > documentPageSize" #footer>
              <el-pagination
                layout="total, prev, pager, next"
                :total="documentTotal"
                :page-size="documentPageSize"
                :current-page="documentPage"
                @current-change="onDocumentPageChange"
              />
            </template>
          </DataPanel>
        </div>
      </el-tab-pane>

      <el-tab-pane v-if="isFullAdmin" label="配置" name="config">
        <div class="config-panel">
          <DataPanel title="知识库配置" subtitle="分块参数留空则跟随全局设置">
            <el-form label-width="130px" class="config-form">
              <el-form-item label="知识库名称" required>
                <el-input v-model="configForm.name" maxlength="160" show-word-limit />
              </el-form-item>
              <el-form-item label="说明">
                <el-input
                  v-model="configForm.description"
                  type="textarea"
                  :rows="2"
                  maxlength="1000"
                  placeholder="可选；留空表示清空说明"
                />
              </el-form-item>
              <el-divider content-position="left">分块参数</el-divider>
              <el-form-item label="父块字符数">
                <el-input-number
                  v-model="configForm.parentChars"
                  :min="1"
                  :step="100"
                  :value-on-clear="null"
                  placeholder="留空跟随全局"
                />
              </el-form-item>
              <el-form-item label="子块字符数">
                <el-input-number
                  v-model="configForm.childChars"
                  :min="1"
                  :step="100"
                  :value-on-clear="null"
                  placeholder="留空跟随全局"
                />
              </el-form-item>
              <el-form-item label="子块重叠数">
                <el-input-number
                  v-model="configForm.overlapChars"
                  :min="0"
                  :step="10"
                  :value-on-clear="null"
                  placeholder="留空跟随全局"
                />
              </el-form-item>
              <el-form-item>
                <span class="config-hint">{{ chunkDefaultsHint }}</span>
              </el-form-item>
              <el-divider content-position="left">模型</el-divider>
              <el-form-item label="Embedding">
                <el-input :model-value="profileName(kb.embedding_model_id)" disabled />
              </el-form-item>
              <el-form-item label="Rerank">
                <el-input :model-value="profileName(kb.rerank_model_id)" disabled />
              </el-form-item>
              <el-form-item>
                <el-alert
                  type="info"
                  :closable="false"
                  show-icon
                  title="模型在大库创建时锁定，已有文档的库不能更换模型；如需更换请新建大库。"
                />
              </el-form-item>
              <el-form-item>
                <el-button
                  type="primary"
                  :loading="configForm.saving"
                  :disabled="kb.status !== 'active'"
                  @click="saveConfig"
                >
                  保存配置
                </el-button>
                <span v-if="kb.status !== 'active'" class="config-hint">知识库已归档，不可修改</span>
              </el-form-item>
            </el-form>
          </DataPanel>
        </div>
      </el-tab-pane>

      <el-tab-pane label="FAQ" name="faqs">
        <FaqPanel :kb-id="kbId" />
      </el-tab-pane>
    </el-tabs>

    <!-- 上传文件（批量多选，或对已有文档上传新版本；必选产品名称与型号） -->
    <el-dialog
      v-model="uploadDialog.visible"
      :title="uploadDialog.mode === 'replace' ? '上传新版本' : '上传文件'"
      width="560px"
      :close-on-click-modal="false"
    >
      <div class="upload-dialog">
        <div
          v-if="uploadDialog.mode === 'replace' && uploadDialog.replaceTarget"
          class="replace-banner"
        >
          <span class="replace-banner-name">{{ uploadDialog.replaceTarget.filename }}</span>
          <el-tag size="small" type="success" effect="light">
            v{{ uploadDialog.replaceTarget.latest_version_number }} →
            v{{ uploadDialog.replaceTarget.latest_version_number + 1 }}
          </el-tag>
          <span class="replace-banner-tip">新文件无需同名，产品标签已按当前文档预填</span>
        </div>
        <div class="upload-field">
          <label class="upload-label">文件<span class="required-mark">*</span></label>
          <div class="upload-row">
            <el-button :icon="'FolderOpened'" @click="pickFile">
              {{ uploadDialog.mode === 'replace' ? '选择新文件' : '选择文件（可多选）' }}
            </el-button>
            <input
              ref="uploadInputRef"
              type="file"
              class="upload-file-input"
              :multiple="uploadDialog.mode === 'batch'"
              @change="onUploadFileChange"
            />
            <span v-if="uploadDialog.items.length > 0" class="upload-count">
              已选 {{ uploadDialog.items.length }} 个
            </span>
          </div>
          <div v-if="uploadDialog.items.length > 0" class="upload-file-list">
            <div v-for="item in uploadDialog.items" :key="item.key" class="upload-file-row">
              <span class="upload-file-dot" :class="`dot-${item.state}`"></span>
              <div class="upload-file-meta">
                <div class="upload-file-name-row">
                  <el-input
                    v-model="item.name"
                    size="small"
                    class="upload-file-rename"
                    maxlength="200"
                    placeholder="入库文件名"
                    :disabled="item.state === 'uploading'"
                  />
                  <span class="upload-file-size">（{{ (item.file.size / 1024).toFixed(1) }} KB）</span>
                </div>
                <div v-if="item.state === 'failed'" class="upload-file-error">{{ item.error }}</div>
                <div
                  v-else-if="item.state === 'pending' && updatingTarget(item.name)"
                  class="upload-file-updating"
                >
                  与库内「{{ updatingTarget(item.name) }}」同名，将更新为新版本
                </div>
                <div v-else-if="item.state === 'uploading'" class="upload-file-progress">
                  <el-progress
                    :percentage="item.progress"
                    :stroke-width="5"
                    :show-text="false"
                  />
                  <span class="upload-file-progress-text">{{ item.progress }}%</span>
                </div>
                <div v-else-if="item.state === 'done'" class="upload-file-done">
                  已上传，等待后台解析
                </div>
              </div>
              <div class="upload-file-actions">
                <el-button
                  v-if="item.state === 'failed'"
                  size="small"
                  text
                  type="primary"
                  @click="retryUploadItem(item)"
                >
                  重试
                </el-button>
                <el-button
                  v-else-if="item.state !== 'uploading'"
                  size="small"
                  text
                  type="danger"
                  @click="removeUploadItem(item)"
                >
                  移除
                </el-button>
              </div>
            </div>
          </div>
        </div>
        <div class="upload-field">
          <el-checkbox v-model="uploadDialog.general" class="general-check">
            通用资料（如公司信息等，不绑定产品名称与型号）
          </el-checkbox>
        </div>
        <div class="upload-field">
          <label class="upload-label">
            产品名称<span v-if="!uploadDialog.general" class="required-mark">*</span>
          </label>
          <el-select
            v-model="uploadDialog.productNames"
            multiple
            filterable
            :disabled="uploadDialog.general"
            placeholder="从全局产品目录中选择，可多选"
            style="width: 100%"
          >
            <el-option v-for="name in productNames" :key="name" :label="name" :value="name" />
          </el-select>
          <span v-if="!uploadDialog.general && productNames.length === 0" class="upload-hint">
            目录暂无产品名称，请先在左侧「产品目录」页登记。
          </span>
        </div>
        <div class="upload-field">
          <label class="upload-label">
            产品型号<span v-if="!uploadDialog.general" class="required-mark">*</span>
          </label>
          <el-select
            v-model="uploadDialog.modelNames"
            multiple
            filterable
            :disabled="uploadDialog.general"
            placeholder="从全局产品目录中选择，可多选"
            style="width: 100%"
          >
            <el-option v-for="name in modelNames" :key="name" :label="name" :value="name" />
          </el-select>
          <span v-if="!uploadDialog.general && modelNames.length === 0" class="upload-hint">
            目录暂无产品型号，请先在左侧「产品目录」页登记。
          </span>
        </div>
        <div v-if="!uploadDialog.general" class="quick-add">
          <el-button v-if="!quickAdd.open" text type="primary" @click="toggleQuickAdd">
            目录里没有？顺手登记一条
          </el-button>
          <div v-else class="quick-add-row">
            <el-input
              v-model="quickAdd.productName"
              maxlength="200"
              placeholder="产品名称，如：体温计"
              class="quick-add-input"
              @keyup.enter="submitQuickAdd"
            />
            <el-input
              v-model="quickAdd.modelName"
              maxlength="200"
              placeholder="产品型号，如：额式体温计"
              class="quick-add-input"
              @keyup.enter="submitQuickAdd"
            />
            <el-button
              type="primary"
              size="small"
              :loading="quickAdd.submitting"
              @click="submitQuickAdd"
            >
              登记并选中
            </el-button>
            <el-button text size="small" @click="toggleQuickAdd">收起</el-button>
          </div>
        </div>
        <p class="upload-hint">
          上传到当前目录：<b>{{ currentFolderId ? '当前选中目录' : '根目录' }}</b
          >。文件名可在列表中直接修改，不写扩展名时沿用原文件扩展名。产品名称与型号从全局产品目录中选择，选中产品名称后只显示对应的产品型号；标签会写入文档元数据，作为检索过滤条件。勾选「通用资料」则不写产品标签，适合公司信息等与具体产品无关的资料。批量上传时以文件名作为文档标识：与库内已有文件同名会更新为新版本，不同名则创建新文档。
        </p>
      </div>
      <template #footer>
        <el-button @click="uploadDialog.visible = false">取消</el-button>
        <el-button
          type="primary"
          :loading="uploadDialog.submitting"
          :disabled="uploadDialog.items.length === 0"
          @click="submitUpload"
        >
          {{ uploadDialog.mode === 'replace' ? '上传新版本' : '上传' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 修改产品标签：覆盖写入，保存后立即影响检索范围 -->
    <el-dialog
      v-model="labelDialog.visible"
      :title="labelDialog.targets.length > 1 ? '批量修改产品标签' : '修改产品标签'"
      width="500px"
      :close-on-click-modal="false"
    >
      <div class="label-dialog">
        <div class="label-target">
          <template v-if="labelDialog.targets.length === 1">
            <span class="label-target-name">{{ labelDialog.targets[0].filename }}</span>
          </template>
          <template v-else>
            <span class="label-target-name">
              将统一覆盖 {{ labelDialog.targets.length }} 个文件的标签
            </span>
            <span class="label-target-names">
              {{ labelDialog.targets.map((document) => document.filename).join('、') }}
            </span>
          </template>
        </div>
        <div class="upload-field">
          <el-checkbox v-model="labelDialog.general" class="general-check">
            通用资料（清空产品名称与型号）
          </el-checkbox>
        </div>
        <div class="upload-field">
          <label class="upload-label">
            产品名称<span v-if="!labelDialog.general" class="required-mark">*</span>
          </label>
          <el-select
            v-model="labelDialog.productNames"
            multiple
            filterable
            :disabled="labelDialog.general"
            placeholder="从全局产品目录中选择，可多选"
            style="width: 100%"
          >
            <el-option v-for="name in productNames" :key="name" :label="name" :value="name" />
          </el-select>
        </div>
        <div class="upload-field">
          <label class="upload-label">
            产品型号<span v-if="!labelDialog.general" class="required-mark">*</span>
          </label>
          <el-select
            v-model="labelDialog.modelNames"
            multiple
            filterable
            :disabled="labelDialog.general"
            placeholder="从全局产品目录中选择，可多选"
            style="width: 100%"
          >
            <el-option v-for="name in labelModelNames" :key="name" :label="name" :value="name" />
          </el-select>
        </div>
        <p class="upload-hint">
          保存后立即影响检索过滤、型号澄清与 FAQ 匹配范围，不会重新解析文件，也不会重建向量。目录里没有需要的标签时，请先到左侧「产品目录」登记。
        </p>
      </div>
      <template #footer>
        <el-button @click="labelDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="labelDialog.submitting" @click="submitLabelUpdate">
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- 移动文档 -->
    <el-dialog v-model="moveDialog.visible" title="移动文档" width="420px">
      <p class="dialog-tip">
        将「{{ moveDialog.document?.filename }}」移动到：
      </p>
      <el-tree
        :data="tree"
        node-key="id"
        highlight-current
        :current-node-key="moveDialog.targetId"
        :expand-on-click-node="false"
        default-expand-all
        @current-change="(node: TreeNode | null) => (moveDialog.targetId = node?.id ?? '')"
      >
        <template #default="{ data }">
          <span class="tree-node"><el-icon><Folder /></el-icon>{{ data.label }}</span>
        </template>
      </el-tree>
      <template #footer>
        <el-button @click="moveDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="moveDialog.submitting" @click="submitMove">移动</el-button>
      </template>
    </el-dialog>

    <!-- 失败文档入库详情 -->
    <el-dialog v-model="ingestionDialog.visible" title="入库详情" width="560px">
      <div v-loading="ingestionDialog.loading" class="ingestion-detail">
        <p class="dialog-tip">{{ ingestionDialog.document?.filename }}</p>
        <template v-if="ingestionDialog.job">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="任务状态">
              {{ statusLabel(ingestionDialog.job.status) }}
            </el-descriptions-item>
            <el-descriptions-item label="处理阶段">
              {{ stageLabel(ingestionDialog.job.stage) }}
            </el-descriptions-item>
            <el-descriptions-item label="尝试次数">
              {{ ingestionDialog.job.attempts }}
            </el-descriptions-item>
            <el-descriptions-item label="自动重试">
              {{ ingestionDialog.job.error_details.retryable ? '已自动排队' : '未自动重试' }}
            </el-descriptions-item>
            <el-descriptions-item label="开始时间">
              {{ ingestionDialog.job.started_at ? formatDateTime(ingestionDialog.job.started_at) : '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="结束时间">
              {{ ingestionDialog.job.finished_at ? formatDateTime(ingestionDialog.job.finished_at) : '—' }}
            </el-descriptions-item>
          </el-descriptions>
          <div class="ingestion-error">
            <div class="ingestion-error-label">错误摘要</div>
            <div class="ingestion-error-message">
              {{ ingestionDialog.job.error_details.message || ingestionDialog.document?.error_message || '无' }}
            </div>
            <div v-if="ingestionDialog.job.error_details.code" class="ingestion-error-code">
              错误码：{{ ingestionDialog.job.error_details.code }}
            </div>
          </div>
        </template>
        <el-empty v-else-if="!ingestionDialog.loading" description="暂无入库任务记录" />
      </div>
      <template #footer>
        <el-button @click="ingestionDialog.visible = false">关闭</el-button>
        <el-button
          v-if="ingestionDialog.document?.status === 'failed'"
          type="warning"
          :loading="retryingId === ingestionDialog.document?.id"
          :disabled="kb?.status !== 'active'"
          @click="ingestionDialog.document && retryDocument(ingestionDialog.document)"
        >
          重试入库
        </el-button>
      </template>
    </el-dialog>

    <!-- 切块预览 -->
    <el-dialog v-model="preview.visible" title="切块预览" width="760px" top="6vh">
      <div class="preview-toolbar">
        <span class="preview-file">{{ preview.document?.filename }}</span>
        <div class="preview-version-bar">
          <el-select
            v-model="preview.versionId"
            size="small"
            style="width: 260px"
            placeholder="选择版本"
            @change="loadChunks(true)"
          >
            <el-option
              v-for="version in preview.versions"
              :key="version.id"
              :label="`v${version.version_number}${version.is_current ? '（当前）' : ''} · ${formatDateTime(version.created_at)}`"
              :value="version.id"
            />
          </el-select>
          <el-tag
            v-if="selectedVersion?.is_current"
            size="small"
            type="success"
            disable-transitions
          >
            当前版本
          </el-tag>
          <el-button
            v-else-if="canWrite && selectedVersion"
            size="small"
            type="primary"
            plain
            :loading="activatingVersionId === selectedVersion.id"
            @click="activateVersion"
          >
            设为当前版本
          </el-button>
        </div>
      </div>
      <div v-if="!preview.loading && preview.parents.length > 0" class="preview-stats">
        共 {{ preview.totalParents }} 个父块 · {{ preview.totalChildren }} 个子块 ·
        父块合计 {{ formatChars(preview.totalChars) }} 字
      </div>
      <div v-loading="preview.loading" class="preview-body">
        <el-empty
          v-if="!preview.loading && preview.parents.length === 0"
          description="该版本还没有切块，等待入库完成后即可预览"
        />
        <el-collapse v-else v-model="preview.activeNames">
          <el-collapse-item
            v-for="parent in preview.parents"
            :key="parent.ordinal"
            :name="String(parent.ordinal)"
          >
            <template #title>
              <span class="chunk-title">
                父块 {{ parent.ordinal }}
                <el-tag
                  v-if="parent.block_type === 'table'"
                  size="small"
                  type="warning"
                  class="page-tag"
                >
                  表格
                </el-tag>
                <el-tag v-if="parent.page_number !== null" size="small" type="info" class="page-tag">
                  第 {{ parent.page_number }} 页
                </el-tag>
                · {{ parent.char_count }} 字
              </span>
            </template>
            <div v-if="parent.section" class="chunk-section">章节：{{ parent.section }}</div>
            <div class="chunk-content" v-html="chunkHtml(parent)" />
            <div v-for="child in parent.children" :key="child.ordinal" class="child-chunk">
              <div class="child-head">子块 {{ child.ordinal }} · {{ child.char_count }} 字</div>
              <div class="chunk-content" v-html="childChunkHtml(child)" />
            </div>
          </el-collapse-item>
        </el-collapse>
        <div
          v-if="preview.parents.length > 0 && preview.parents.length < preview.totalParents"
          class="preview-more"
        >
          <el-button size="small" :loading="preview.loadingMore" @click="loadChunks(false)">
            加载更多父块（已显示 {{ preview.parents.length }} / {{ preview.totalParents }}）
          </el-button>
        </div>
      </div>
    </el-dialog>

    <!-- 原文件预览 -->
    <DocumentPreviewDrawer
      v-model="previewVisible"
      :item="previewItem"
      @download="downloadFile"
    />
  </div>
</template>

<style scoped>
.header-meta {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}
.detail-tabs :deep(.el-tabs__header) {
  margin: 0 0 12px;
}
.tab-alert {
  margin-bottom: 12px;
}
.files-layout {
  display: flex;
  align-items: stretch;
  gap: 14px;
  min-height: 420px;
}
.folder-panel {
  width: 260px;
  flex-shrink: 0;
}
.tree-body {
  padding: 10px 12px 14px;
  max-height: 560px;
  overflow: auto;
}
.document-panel {
  flex: 1;
  min-width: 0;
}
/* 操作按钮保持单行：收紧文字按钮的横向留白与间距 */
.file-actions {
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
}
.file-actions :deep(.el-button) {
  margin-left: 0;
  padding-left: 5px;
  padding-right: 5px;
}
.file-actions :deep(.el-button + .el-button) {
  margin-left: 6px;
}
.document-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 18px 10px;
}
.document-search {
  width: 280px;
}
.tree-node {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 1;
  min-width: 0;
}
.node-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.node-more {
  visibility: hidden;
  cursor: pointer;
}
.tree-node:hover .node-more {
  visibility: visible;
}
@media (max-width: 900px) {
  .files-layout {
    flex-direction: column;
  }
  .folder-panel {
    width: auto;
  }
  .tree-body {
    max-height: 220px;
  }
}
.dialog-tip {
  margin: 0 0 8px;
  color: var(--el-text-color-secondary);
}
.upload-dialog {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.upload-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.upload-label {
  font-size: 13px;
  color: var(--el-text-color-primary);
}
.required-mark {
  color: var(--el-color-danger);
  margin-left: 2px;
}
.upload-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.upload-file-input {
  /* 隐藏原生选择框，由「选择文件」按钮代为触发；opacity 保留可聚焦性。 */
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
}
.upload-file-name-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.upload-file-rename {
  flex: 1;
}
.upload-count {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.replace-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-8);
  border-radius: var(--el-border-radius-base);
  padding: 12px 14px;
  color: var(--el-color-primary);
}
.replace-banner-name {
  font-weight: 600;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 260px;
}
.replace-banner-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-left: auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.upload-file-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 180px;
  overflow-y: auto;
  background: var(--el-fill-color-lighter);
  border-radius: 10px;
  padding: 8px;
}
.upload-file-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 4px 6px;
}
.upload-file-dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-top: 5px;
  background: var(--el-color-info);
}
.dot-uploading {
  background: var(--el-color-primary);
  animation: upload-dot-pulse 1s ease-in-out infinite;
}
.dot-done {
  background: var(--el-color-success);
}
.dot-failed {
  background: var(--el-color-danger);
}
@keyframes upload-dot-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.35;
  }
}
.upload-file-meta {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.upload-file-size {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.upload-file-error {
  font-size: 12px;
  color: var(--el-color-danger);
}
.upload-file-updating {
  font-size: 12px;
  color: var(--el-color-primary);
}
.upload-file-done {
  font-size: 12px;
  color: var(--el-color-success);
}
.upload-file-progress {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}
.upload-file-progress :deep(.el-progress) {
  flex: 1;
}
.upload-file-progress-text {
  flex: none;
  min-width: 34px;
  text-align: right;
  font-size: 12px;
  color: var(--el-color-primary);
  font-variant-numeric: tabular-nums;
}
.upload-file-actions {
  flex: none;
  display: flex;
  align-items: center;
}
.upload-file-actions .el-button + .el-button {
  margin-left: 0;
}
.upload-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
.preview-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.preview-version-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}
.preview-file {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}
.preview-body {
  max-height: 62vh;
  overflow-y: auto;
}
.preview-stats {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 10px;
}
.preview-more {
  text-align: center;
  padding: 12px 0 4px;
}
.chunk-section {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 4px;
}
.chunk-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.chunk-content {
  margin: 8px 0;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.7;
  color: var(--el-text-color-primary);
  font-size: 13px;
}
.chunk-content :deep(img.md-image) {
  display: inline-block;
  max-width: 100%;
  max-height: 260px;
  border-radius: 12px;
  border: 1px solid var(--el-border-color-lighter);
  margin: 6px 4px 2px 0;
  vertical-align: middle;
}
.chunk-content :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
  width: 100%;
  font-size: 13px;
  white-space: normal;
}
.chunk-content :deep(th),
.chunk-content :deep(td) {
  border: 1px solid var(--el-border-color-lighter);
  padding: 6px 10px;
  text-align: left;
  vertical-align: top;
  word-break: break-word;
}
.chunk-content :deep(th) {
  background: var(--el-fill-color-light);
  font-weight: 600;
}
.chunk-content :deep(td img.md-image) {
  max-height: 120px;
  margin: 2px 4px 2px 0;
}
.child-chunk {
  border-left: 3px solid var(--el-color-primary-light-5);
  padding-left: 10px;
  margin: 10px 0 10px 12px;
}
.child-head {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.status-cell {
  display: flex;
  align-items: center;
  gap: 2px;
}
.label-muted {
  color: var(--el-text-color-placeholder);
}
/* 标签单元格：文本截断，编辑入口平时淡显，悬停行内变清晰。 */
.label-cell {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}
.label-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.label-edit {
  flex: none;
  padding: 0 2px;
  color: var(--el-text-color-placeholder);
  opacity: 0.35;
  transition: opacity 0.15s ease, color 0.15s ease;
}
.el-table__row:hover .label-edit,
.label-edit:focus-visible {
  color: var(--el-color-primary);
  opacity: 1;
}
.label-target {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 12px;
  margin-bottom: 14px;
  border-radius: 10px;
  background: var(--el-fill-color-lighter);
  border-left: 3px solid var(--el-color-primary-light-5);
}
.label-target-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--el-text-color-primary);
}
.label-target-names {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ingestion-detail {
  min-height: 120px;
}
.ingestion-error {
  margin-top: 12px;
  padding: 10px 12px;
  border-radius: 10px;
  background: var(--el-color-danger-light-9);
  border: 1px solid var(--el-color-danger-light-7);
}
.ingestion-error-label {
  font-size: 12px;
  color: var(--el-color-danger);
  margin-bottom: 4px;
}
.ingestion-error-message {
  font-size: 13px;
  line-height: 1.6;
  word-break: break-word;
  color: var(--el-text-color-primary);
}
.ingestion-error-code {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  font-family: var(--el-font-family-monospace, monospace);
}
.config-panel {
  max-width: 760px;
}
.quick-add {
  margin: 2px 0 10px;
}
.quick-add-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.quick-add-input {
  width: 180px;
}
.config-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
