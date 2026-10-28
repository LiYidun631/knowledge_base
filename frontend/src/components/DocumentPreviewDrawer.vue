<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import axios from 'axios'
import { client } from '../api/client'
import { renderMarkdown } from '../utils/markdown'

interface PreviewDocument {
  id: string
  filename: string
  latest_version_number: number
}

type PreviewMode =
  | 'loading'
  | 'pdf'
  | 'image'
  | 'html'
  | 'text'
  | 'markdown'
  | 'docx'
  | 'sheet'
  | 'slides'
  | 'unsupported'

const props = defineProps<{
  modelValue: boolean
  item: PreviewDocument | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  download: [item: PreviewDocument]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const FORMAT_LABELS: Record<string, string> = {
  pdf: 'PDF',
  doc: 'DOC',
  docx: 'DOCX',
  docm: 'DOCM',
  rtf: 'RTF',
  odt: 'ODT',
  xls: 'XLS',
  xlsx: 'XLSX',
  xlsm: 'XLSM',
  ods: 'ODS',
  csv: 'CSV',
  ppt: 'PPT',
  pptx: 'PPTX',
  pptm: 'PPTM',
  odp: 'ODP',
  txt: 'TXT',
  md: 'MD',
  log: 'LOG',
  json: 'JSON',
  xml: 'XML',
  html: 'HTML',
  htm: 'HTML',
  png: 'PNG',
  jpg: 'JPG',
  jpeg: 'JPEG',
  gif: 'GIF',
  bmp: 'BMP',
  webp: 'WEBP',
  tif: 'TIFF',
  tiff: 'TIFF',
}

const DOCX_EXTENSIONS = new Set(['docx', 'docm'])
const SHEET_EXTENSIONS = new Set(['xlsx', 'xlsm', 'xls', 'ods', 'csv'])
const SLIDES_EXTENSIONS = new Set(['pptx', 'pptm'])
const SERVER_EXTENSIONS = new Set(['doc', 'ppt', 'rtf', 'odt', 'odp', 'tif', 'tiff'])
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'])
const TEXT_EXTENSIONS = new Set(['txt', 'log', 'json', 'xml'])

const mode = ref<PreviewMode>('loading')
const loading = ref(false)
const errorText = ref('')
const blobUrl = ref('')
const textContent = ref('')
const markdownHtml = ref('')
const sheetNames = ref<string[]>([])
const sheetHtml = ref<string[]>([])
const activeSheet = ref(0)
const docxHost = ref<HTMLElement | null>(null)
const slidesHost = ref<HTMLElement | null>(null)

const formatLabel = computed(() => {
  const extension = extensionOf(props.item?.filename ?? '')
  return FORMAT_LABELS[extension] ?? (extension ? extension.toUpperCase() : 'FILE')
})

const modeLabel = computed(() => {
  switch (mode.value) {
    case 'docx':
      return 'Word 版式预览'
    case 'sheet':
      return '表格预览'
    case 'slides':
      return '逐页预览'
    case 'pdf':
      return 'PDF 预览'
    case 'image':
      return '图片预览'
    case 'markdown':
      return 'Markdown 预览'
    case 'text':
      return '文本预览'
    case 'html':
      return '网页预览'
    case 'loading':
      return '正在准备'
    default:
      return '暂不可预览'
  }
})

const fallbackNote = computed(() =>
  errorText.value.includes('LibreOffice')
    ? '也可下载原文件后用本地 Office 打开；文件入库与检索不受影响。'
    : '文件仍可正常下载、切块与检索，不影响知识库内容。',
)

let requestSeq = 0
let pptxViewer: { destroy: () => void } | null = null

function extensionOf(filename: string): string {
  const index = filename.lastIndexOf('.')
  return index >= 0 ? filename.slice(index + 1).toLowerCase() : ''
}

async function fetchFile(item: PreviewDocument): Promise<Blob> {
  const { data } = await client.get<Blob>(`/documents/${item.id}/file`, {
    params: { disposition: 'inline' },
    responseType: 'blob',
  })
  return data
}

async function fetchServerPreview(item: PreviewDocument): Promise<Blob> {
  const { data } = await client.get<Blob>(`/documents/${item.id}/preview`, {
    responseType: 'blob',
  })
  return data
}

async function previewErrorMessage(error: unknown, fallback: string): Promise<string> {
  if (axios.isAxiosError(error)) {
    let data: unknown = error.response?.data
    if (data instanceof Blob) {
      const raw = await data.text()
      try {
        data = JSON.parse(raw)
      } catch {
        data = raw
      }
    }
    if (typeof data === 'string' && data.trim()) return data
    if (
      data &&
      typeof data === 'object' &&
      typeof (data as { detail?: unknown }).detail === 'string'
    ) {
      return (data as { detail: string }).detail
    }
    if (error.code === 'ECONNABORTED') return '预览加载超时，请下载后查看'
  }
  if (error instanceof Error && error.message === '预览容器未就绪') return error.message
  return fallback
}

function clearContent() {
  if (blobUrl.value) {
    URL.revokeObjectURL(blobUrl.value)
    blobUrl.value = ''
  }
  textContent.value = ''
  markdownHtml.value = ''
  sheetNames.value = []
  sheetHtml.value = []
  activeSheet.value = 0
  pptxViewer?.destroy()
  pptxViewer = null
  if (docxHost.value) docxHost.value.innerHTML = ''
  if (slidesHost.value) slidesHost.value.innerHTML = ''
  errorText.value = ''
}

function reset() {
  requestSeq += 1
  loading.value = false
  clearContent()
  mode.value = 'loading'
}

async function load(item: PreviewDocument) {
  const current = ++requestSeq
  loading.value = true
  mode.value = 'loading'
  clearContent()
  await nextTick()

  const extension = extensionOf(item.filename)
  try {
    if (DOCX_EXTENSIONS.has(extension)) {
      await loadDocx(item, current)
    } else if (SHEET_EXTENSIONS.has(extension)) {
      await loadSheet(item, current)
    } else if (SLIDES_EXTENSIONS.has(extension)) {
      await loadSlides(item, current)
    } else if (SERVER_EXTENSIONS.has(extension)) {
      await loadServerPreview(item, current)
    } else if (extension === 'pdf') {
      await loadBlobPreview(item, current, 'pdf')
    } else if (IMAGE_EXTENSIONS.has(extension)) {
      await loadBlobPreview(item, current, 'image')
    } else if (extension === 'html' || extension === 'htm') {
      await loadBlobPreview(item, current, 'html')
    } else if (extension === 'md') {
      const blob = await fetchFile(item)
      if (current !== requestSeq) return
      // 文档预览按原文层级渲染，不像问答答案那样整体降两级标题。
      markdownHtml.value = renderMarkdown(await blob.text(), 0)
      mode.value = 'markdown'
    } else if (TEXT_EXTENSIONS.has(extension)) {
      const blob = await fetchFile(item)
      if (current !== requestSeq) return
      textContent.value = await blob.text()
      mode.value = 'text'
    } else {
      errorText.value = '该格式暂不支持在线预览'
      mode.value = 'unsupported'
    }
  } catch (error) {
    if (current !== requestSeq) return
    errorText.value = await previewErrorMessage(error, '预览加载失败，请下载后查看')
    mode.value = 'unsupported'
  } finally {
    if (current === requestSeq) loading.value = false
  }
}

async function loadBlobPreview(
  item: PreviewDocument,
  current: number,
  target: 'pdf' | 'image' | 'html',
) {
  const blob = await fetchFile(item)
  if (current !== requestSeq) return
  blobUrl.value = URL.createObjectURL(blob)
  mode.value = target
}

async function loadServerPreview(item: PreviewDocument, current: number) {
  const blob = await fetchServerPreview(item)
  if (current !== requestSeq) return
  blobUrl.value = URL.createObjectURL(blob)
  mode.value = blob.type.startsWith('image/') ? 'image' : 'pdf'
}

async function loadDocx(item: PreviewDocument, current: number) {
  const blob = await fetchFile(item)
  if (current !== requestSeq) return
  mode.value = 'docx'
  await nextTick()
  if (current !== requestSeq) return
  const host = docxHost.value
  if (!host) throw new Error('预览容器未就绪')
  try {
    const { renderAsync } = await import('docx-preview')
    await renderAsync(blob, host, undefined, {
      className: 'kb-docx',
      inWrapper: true,
      breakPages: true,
      ignoreWidth: false,
      ignoreHeight: false,
      ignoreFonts: false,
      renderHeaders: true,
      renderFooters: true,
      useBase64URL: true,
    })
    if (current !== requestSeq) host.innerHTML = ''
  } catch {
    if (current !== requestSeq) return
    host.innerHTML = ''
    await loadServerPreview(item, current)
  }
}

async function loadSheet(item: PreviewDocument, current: number) {
  const blob = await fetchFile(item)
  if (current !== requestSeq) return
  const XLSX = await import('@e965/xlsx')
  const workbook = XLSX.read(new Uint8Array(await blob.arrayBuffer()), {
    type: 'array',
    cellDates: true,
  })
  if (current !== requestSeq) return
  const names = workbook.SheetNames
  if (names.length === 0) throw new Error('工作簿中没有可预览的工作表')
  const htmls = names.map((name) => {
    const raw = XLSX.utils.sheet_to_html(workbook.Sheets[name], {
      editable: false,
      header: '',
      footer: '',
    })
    const table = new DOMParser().parseFromString(raw, 'text/html').querySelector('table')
    return table ? table.outerHTML : '<p class="sheet-empty">空工作表</p>'
  })
  if (current !== requestSeq) return
  sheetNames.value = names
  sheetHtml.value = htmls
  activeSheet.value = 0
  mode.value = 'sheet'
}

async function loadSlides(item: PreviewDocument, current: number) {
  const blob = await fetchFile(item)
  if (current !== requestSeq) return
  mode.value = 'slides'
  await nextTick()
  if (current !== requestSeq) return
  const host = slidesHost.value
  if (!host) throw new Error('预览容器未就绪')
  host.innerHTML = ''
  try {
    const { PptxViewer, RECOMMENDED_ZIP_LIMITS } = await import('@aiden0z/pptx-renderer')
    pptxViewer = await PptxViewer.open(await blob.arrayBuffer(), host, {
      width: Math.max(240, host.clientWidth - 24),
      fitMode: 'contain',
      scrollContainer: host,
      zipLimits: RECOMMENDED_ZIP_LIMITS,
      // 预览不受信任的上传文件时不发起外部媒体请求。
      externalMedia: 'block',
      lazyMedia: true,
      listOptions: { windowed: true, showSlideLabels: true },
    })
    if (current !== requestSeq) {
      pptxViewer?.destroy()
      pptxViewer = null
      host.innerHTML = ''
    }
  } catch {
    if (current !== requestSeq) return
    pptxViewer?.destroy()
    pptxViewer = null
    host.innerHTML = ''
    await loadServerPreview(item, current)
  }
}

// 打开与切换文件合并成一次监听：分开监听会在打开瞬间同时命中，重复请求两次。
watch(
  () => [props.modelValue, props.item?.id] as const,
  ([open]) => {
    if (!open) {
      reset()
      return
    }
    if (props.item) void load(props.item)
  },
)

onBeforeUnmount(reset)
</script>

<template>
  <el-drawer
    v-model="visible"
    :with-header="false"
    size="78%"
    class="doc-preview-drawer"
    @closed="reset"
  >
    <div class="preview-head">
      <div class="preview-heading">
        <div class="preview-mark">{{ formatLabel }}</div>
        <div class="preview-heading-text">
          <div class="preview-title" :title="item?.filename">
            {{ item?.filename || '原文件预览' }}
          </div>
          <div class="preview-meta">
            <span v-if="item">v{{ item.latest_version_number }}</span>
            <span v-if="item" class="preview-dot" />
            <span>{{ modeLabel }}</span>
          </div>
        </div>
      </div>
      <div class="preview-actions">
        <el-button v-if="item" text bg @click="emit('download', item)">
          <el-icon><Download /></el-icon>
          <span>下载</span>
        </el-button>
        <el-button text circle @click="visible = false">
          <el-icon><Close /></el-icon>
        </el-button>
      </div>
    </div>

    <div class="preview-body" v-loading="loading" element-loading-text="正在准备预览">
      <div v-if="mode === 'docx'" ref="docxHost" class="docx-canvas" />
      <div v-else-if="mode === 'slides'" ref="slidesHost" class="slides-canvas" />
      <div v-else-if="mode === 'sheet'" class="sheet-shell">
        <div v-if="sheetNames.length > 1" class="sheet-tabs">
          <button
            v-for="(name, index) in sheetNames"
            :key="name"
            type="button"
            class="sheet-tab"
            :class="{ active: index === activeSheet }"
            @click="activeSheet = index"
          >
            {{ name }}
          </button>
        </div>
        <div class="sheet-surface" v-html="sheetHtml[activeSheet]" />
      </div>
      <iframe
        v-else-if="mode === 'pdf'"
        :src="`${blobUrl}#view=FitH`"
        class="preview-frame"
        title="PDF 预览"
      />
      <iframe
        v-else-if="mode === 'html'"
        :src="blobUrl"
        class="preview-frame preview-html-frame"
        sandbox=""
        referrerpolicy="no-referrer"
        title="HTML 预览"
      />
      <div v-else-if="mode === 'image'" class="image-stage">
        <img :src="blobUrl" :alt="item?.filename || '图片预览'" />
      </div>
      <pre v-else-if="mode === 'text'" class="text-sheet">{{ textContent }}</pre>
      <article
        v-else-if="mode === 'markdown'"
        class="markdown-sheet preview-markdown"
        v-html="markdownHtml"
      />
      <div v-else-if="mode === 'loading'" class="preview-loading">
        <el-skeleton :rows="7" animated />
      </div>
      <div v-else class="preview-fallback">
        <div class="fallback-mark">{{ formatLabel }}</div>
        <div class="fallback-title">{{ errorText || '该格式暂不支持在线预览' }}</div>
        <div class="fallback-note">{{ fallbackNote }}</div>
        <el-button v-if="item" type="primary" @click="emit('download', item)">
          下载原文件
        </el-button>
      </div>
    </div>
  </el-drawer>
</template>

<style>
.doc-preview-drawer .el-drawer__body {
  padding: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.doc-preview-drawer .preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 16px 22px;
  border-bottom: 1px solid var(--el-border-color-light);
  background: rgba(255, 255, 255, 0.94);
}

.doc-preview-drawer .preview-heading {
  display: flex;
  align-items: center;
  gap: 13px;
  min-width: 0;
}

.doc-preview-drawer .preview-mark {
  width: 46px;
  height: 46px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: 12px;
  border: 1px solid var(--el-color-primary-light-8);
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
}

.doc-preview-drawer .preview-heading-text {
  min-width: 0;
}

.doc-preview-drawer .preview-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.doc-preview-drawer .preview-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.doc-preview-drawer .preview-dot {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: var(--el-text-color-placeholder);
}

.doc-preview-drawer .preview-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}

.doc-preview-drawer .preview-actions .el-button span {
  margin-left: 4px;
}

.doc-preview-drawer .preview-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 22px;
  background:
    radial-gradient(circle at 14% 0%, rgba(79, 70, 229, 0.05), transparent 34%),
    linear-gradient(180deg, #f7f7fa 0%, #f1f2f6 100%);
}

.doc-preview-drawer .preview-frame {
  display: block;
  box-sizing: border-box;
  width: 100%;
  height: 100%;
  min-height: 560px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 8px 26px rgba(15, 23, 42, 0.06);
}

.doc-preview-drawer .preview-html-frame {
  min-height: 620px;
}

.doc-preview-drawer .image-stage {
  min-height: 100%;
  display: grid;
  place-items: center;
  padding: 10px;
}

.doc-preview-drawer .image-stage img {
  max-width: 100%;
  max-height: calc(100vh - 170px);
  border-radius: 10px;
  background: #fff;
  box-shadow: 0 8px 26px rgba(15, 23, 42, 0.08);
}

.doc-preview-drawer .text-sheet,
.doc-preview-drawer .markdown-sheet {
  max-width: 920px;
  margin: 0 auto;
  padding: 26px 30px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 12px;
  background: #fff;
  color: var(--el-text-color-primary);
  box-shadow: 0 8px 26px rgba(15, 23, 42, 0.05);
}

.doc-preview-drawer .text-sheet {
  margin: 0 auto;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: 'Cascadia Code', 'SFMono-Regular', Consolas, 'Liberation Mono', monospace;
  font-size: 12.5px;
  line-height: 1.75;
}

.doc-preview-drawer .preview-markdown {
  font-size: 14px;
  line-height: 1.8;
}

.doc-preview-drawer .preview-markdown h1,
.doc-preview-drawer .preview-markdown h2,
.doc-preview-drawer .preview-markdown h3 {
  margin: 22px 0 10px;
  line-height: 1.4;
}

.doc-preview-drawer .preview-markdown h1 {
  font-size: 21px;
}

.doc-preview-drawer .preview-markdown h2 {
  font-size: 18px;
}

.doc-preview-drawer .preview-markdown h3 {
  font-size: 15px;
}

.doc-preview-drawer .preview-markdown p {
  margin: 0 0 12px;
}

.doc-preview-drawer .preview-markdown ul,
.doc-preview-drawer .preview-markdown ol {
  margin: 0 0 14px;
  padding-left: 22px;
}

.doc-preview-drawer .preview-markdown table {
  width: 100%;
  border-collapse: collapse;
  margin: 14px 0;
  font-size: 13px;
}

.doc-preview-drawer .preview-markdown th,
.doc-preview-drawer .preview-markdown td {
  border: 1px solid var(--el-border-color-light);
  padding: 7px 10px;
  text-align: left;
}

.doc-preview-drawer .preview-markdown th {
  background: var(--el-fill-color-light);
  font-weight: 600;
}

.doc-preview-drawer .docx-canvas {
  max-width: 100%;
  padding-bottom: 24px;
}

/* docx-preview 会按 className 给默认类名加前缀，这里对应 kb-docx-wrapper / section.kb-docx。 */
.doc-preview-drawer .docx-canvas .kb-docx-wrapper {
  background: transparent;
  padding: 12px 0 24px;
}

.doc-preview-drawer .docx-canvas .kb-docx-wrapper > section.kb-docx {
  margin: 0 auto 18px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
  box-shadow: 0 8px 26px rgba(15, 23, 42, 0.07);
}

.doc-preview-drawer .sheet-shell {
  display: flex;
  flex-direction: column;
  min-height: 100%;
  border: 1px solid var(--el-border-color-light);
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 8px 26px rgba(15, 23, 42, 0.05);
  overflow: hidden;
}

.doc-preview-drawer .sheet-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--el-border-color-light);
  background: var(--el-fill-color-lighter);
  overflow-x: auto;
}

.doc-preview-drawer .sheet-tab {
  flex: none;
  max-width: 220px;
  padding: 6px 12px;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: var(--el-text-color-regular);
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: background 0.15s var(--kb-ease), color 0.15s var(--kb-ease);
}

.doc-preview-drawer .sheet-tab:hover {
  background: var(--el-fill-color);
}

.doc-preview-drawer .sheet-tab.active {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-weight: 600;
}

.doc-preview-drawer .sheet-surface {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 4px;
}

.doc-preview-drawer .sheet-surface table {
  border-collapse: collapse;
  font-size: 12px;
  color: var(--el-text-color-regular);
}

.doc-preview-drawer .sheet-surface th,
.doc-preview-drawer .sheet-surface td {
  border: 1px solid var(--el-border-color-lighter);
  padding: 6px 10px;
  min-width: 54px;
  white-space: pre-wrap;
  vertical-align: top;
}

.doc-preview-drawer .sheet-surface thead th,
.doc-preview-drawer .sheet-surface tr:first-child td {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--el-fill-color-light);
  font-weight: 600;
}

.doc-preview-drawer .slides-canvas {
  height: 100%;
  min-height: 560px;
  overflow: auto;
  padding: 4px;
}

.doc-preview-drawer .preview-loading {
  max-width: 920px;
  margin: 0 auto;
  padding: 26px 30px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 12px;
  background: #fff;
}

.doc-preview-drawer .preview-fallback {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 40px 20px;
  text-align: center;
}

.doc-preview-drawer .fallback-mark {
  width: 74px;
  height: 74px;
  display: grid;
  place-items: center;
  margin-bottom: 6px;
  border-radius: 18px;
  border: 1px solid var(--el-color-primary-light-8);
  background:
    linear-gradient(135deg, var(--el-color-primary-light-9), #fff 70%);
  color: var(--el-color-primary);
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0.06em;
  clip-path: polygon(0 0, 72% 0, 100% 28%, 100% 100%, 0 100%);
}

.doc-preview-drawer .fallback-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.doc-preview-drawer .fallback-note {
  max-width: 420px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--el-text-color-secondary);
}

.doc-preview-drawer .preview-fallback .el-button {
  margin-top: 6px;
}
</style>
