<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { client, errorMessage } from '../api/client'
import { canWrite } from '../api/session'
import { renderMarkdown } from '../utils/markdown'
import DataPanel from './ui/DataPanel.vue'
import FilterBar from './ui/FilterBar.vue'
import StatusPill from './ui/StatusPill.vue'
import EmptyState from './ui/EmptyState.vue'

interface FaqEntry {
  id: string
  knowledge_base_id: string
  document_id: string | null
  document_version_id: string | null
  question: string
  answer: string
  source: 'auto' | 'manual'
  created_at: string
  updated_at: string
}

interface DocOption {
  id: string
  filename: string
}

interface FaqListResponse {
  page: number
  page_size: number
  total: number
  items: FaqEntry[]
}

const props = defineProps<{ kbId: string; initialQuery?: string }>()

const faqs = ref<FaqEntry[]>([])
const documents = ref<DocOption[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
// scope 同时决定列表过滤范围与新增 FAQ 的归属。
const scope = ref<'all' | 'kb' | 'document'>('all')
const documentId = ref('')
const sourceFilter = ref<'' | 'auto' | 'manual'>('')
const q = ref('')
const loading = ref(false)
const selectedId = ref('')

const selected = computed(() => faqs.value.find((f) => f.id === selectedId.value) ?? null)

const sourceMeta: Record<string, { label: string; type: 'success' | 'warning' }> = {
  auto: { label: '自动生成', type: 'success' },
  manual: { label: '手动添加', type: 'warning' },
}

// 列表空态区分「筛不到」与「本来就没有」，文案和图形灰度都不同。
const isFiltered = computed(() => Boolean(q.value.trim() || sourceFilter.value))

const emptyTitle = computed(() => {
  if (isFiltered.value) return '没有符合条件的 FAQ'
  if (scope.value === 'document' && !documentId.value) return '该知识库还没有文档'
  return '暂无 FAQ'
})

const emptyHint = computed(() => {
  if (isFiltered.value) return '换个关键词，或清空来源筛选。'
  if (scope.value === 'kb') return '点击工具栏「添加」创建库级 FAQ。'
  if (scope.value === 'document' && !documentId.value) return '先上传文档，再按文档生成 FAQ。'
  return '上传文档后会自动生成，也可手动添加。'
})

function docName(id: string | null): string {
  if (!id) return '库级 FAQ'
  return documents.value.find((d) => d.id === id)?.filename ?? '未知文档'
}

function formatDateTime(value: string): string {
  return value.slice(0, 19).replace('T', ' ')
}

async function load() {
  if (scope.value === 'document' && !documentId.value) {
    faqs.value = []
    total.value = 0
    return
  }
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: page.value,
      page_size: pageSize,
      scope: scope.value,
    }
    if (scope.value === 'document') params.document_id = documentId.value
    if (sourceFilter.value) params.source = sourceFilter.value
    if (q.value.trim()) params.q = q.value.trim()
    const { data } = await client.get<FaqListResponse>(
      `/admin/knowledge-bases/${props.kbId}/faqs`,
      { params },
    )
    faqs.value = data.items
    total.value = data.total
    if (!data.items.some((f) => f.id === selectedId.value)) {
      selectedId.value = data.items[0]?.id ?? ''
    }
  } catch (error) {
    errorMessage(error, '加载 FAQ 失败')
  } finally {
    loading.value = false
  }
}

async function loadDocuments() {
  try {
    const { data } = await client.get<{ id: string; filename: string }[]>(
      `/knowledge-bases/${props.kbId}/documents`,
      { params: { limit: 200 } },
    )
    documents.value = data.map((d) => ({ id: d.id, filename: d.filename }))
  } catch (error) {
    errorMessage(error, '加载文档列表失败')
  }
}

function applyFilters() {
  page.value = 1
  load()
}

function onScopeChange() {
  if (scope.value === 'document' && !documentId.value && documents.value.length > 0) {
    documentId.value = documents.value[0].id
  }
  applyFilters()
}

function onDocumentChange() {
  applyFilters()
}

// —— 新增/编辑 ——

const dialogVisible = ref(false)
const editing = ref<FaqEntry | null>(null)
const form = reactive({ question: '', answer: '' })
const saving = ref(false)

function openAdd() {
  editing.value = null
  form.question = ''
  form.answer = ''
  dialogVisible.value = true
}

function openEdit(entry: FaqEntry) {
  editing.value = entry
  form.question = entry.question
  form.answer = entry.answer
  dialogVisible.value = true
}

async function save() {
  const question = form.question.trim()
  const answer = form.answer.trim()
  if (!question || !answer) {
    ElMessage.warning('问题与答案都不能为空')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      const { data } = await client.patch<FaqEntry>(`/admin/faqs/${editing.value.id}`, {
        question,
        answer,
      })
      Object.assign(editing.value, data)
      ElMessage.success('FAQ 已保存')
    } else {
      await client.post<FaqEntry>(`/admin/knowledge-bases/${props.kbId}/faqs`, {
        question,
        answer,
        document_id: scope.value === 'document' ? documentId.value : null,
      })
      ElMessage.success('FAQ 已添加')
      page.value = 1
    }
    dialogVisible.value = false
    await load()
  } catch (error) {
    errorMessage(error, '保存 FAQ 失败')
  } finally {
    saving.value = false
  }
}

// —— AI 改写 ——

const rewriteVisible = ref(false)
const rewriteInstruction = ref('')
const rewriting = ref(false)

function openRewrite() {
  rewriteInstruction.value = ''
  rewriteVisible.value = true
}

async function rewrite() {
  if (!selected.value) return
  rewriting.value = true
  try {
    const { data } = await client.post<FaqEntry>(`/admin/faqs/${selected.value.id}/rewrite`, {
      instruction: rewriteInstruction.value.trim() || null,
    })
    Object.assign(selected.value, data)
    rewriteVisible.value = false
    ElMessage.success('答案已改写')
  } catch (error) {
    errorMessage(error, 'AI 改写失败')
  } finally {
    rewriting.value = false
  }
}

// —— 删除 ——

async function remove(entry: FaqEntry) {
  try {
    await ElMessageBox.confirm(
      `删除后在线问答将不再直出这条 FAQ。确认删除「${entry.question}」？`,
      '删除 FAQ',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/admin/faqs/${entry.id}`)
    if (selectedId.value === entry.id) selectedId.value = ''
    ElMessage.success('已删除')
    await load()
    // 删除当前页最后一条时回退一页，避免停留在空页。
    if (faqs.value.length === 0 && page.value > 1) {
      page.value -= 1
      await load()
    }
  } catch (error) {
    errorMessage(error, '删除 FAQ 失败')
  }
}

// —— 文档自动生成 ——

const generating = ref(false)
const generateDoc = computed(() =>
  scope.value === 'document'
    ? documents.value.find((d) => d.id === documentId.value) ?? null
    : null,
)

async function generate() {
  const doc = generateDoc.value
  if (!doc) return
  try {
    await ElMessageBox.confirm(
      `将为「${doc.filename}」重新提炼自动生成 FAQ，该文档旧的自动生成条目会被替换（手动添加的条目不受影响）。`,
      '生成文档 FAQ',
      { confirmButtonText: '开始生成', cancelButtonText: '取消', type: 'info' },
    )
  } catch {
    return
  }
  generating.value = true
  try {
    const { data } = await client.post<{ entries: FaqEntry[] }>(
      `/admin/documents/${doc.id}/faqs/generate`,
    )
    ElMessage.success(`已生成 ${data.entries.length} 条 FAQ`)
    page.value = 1
    await load()
  } catch (error) {
    errorMessage(error, '生成 FAQ 失败')
  } finally {
    generating.value = false
  }
}

const batchGenerating = ref(false)

async function generateBatch() {
  try {
    await ElMessageBox.confirm(
      '将为当前知识库中缺少自动 FAQ 的文档排队生成；已有条目或已在排队的文档会自动跳过。生成在后台异步执行，稍后刷新查看结果。',
      '批量补齐文档 FAQ',
      { confirmButtonText: '开始排队', cancelButtonText: '取消', type: 'info' },
    )
  } catch {
    return
  }
  batchGenerating.value = true
  try {
    const { data } = await client.post<{ queued: number; skipped: number }>(
      `/admin/knowledge-bases/${props.kbId}/faqs/generate`,
    )
    ElMessage.success(`已入队 ${data.queued} 篇，跳过 ${data.skipped} 篇`)
  } catch (error) {
    errorMessage(error, '批量生成 FAQ 失败')
  } finally {
    batchGenerating.value = false
  }
}

onMounted(async () => {
  if (!props.kbId) return
  // 全局搜索跳转过来时预填关键词，省去重输。
  if (props.initialQuery) q.value = props.initialQuery
  await loadDocuments()
  await load()
})
</script>

<template>
  <div class="faq-panel">
    <div class="panel-bar">
      <FilterBar>
        <slot name="toolbar" />
        <template v-if="kbId">
          <el-input
            v-model="q"
            clearable
            size="small"
            placeholder="搜索问题或答案"
            class="search-input"
            @keyup.enter="applyFilters"
            @clear="applyFilters"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-select v-model="scope" size="small" class="scope-select" @change="onScopeChange">
            <el-option label="全部范围" value="all" />
            <el-option label="仅库级 FAQ" value="kb" />
            <el-option label="指定文档" value="document" />
          </el-select>
          <el-select
            v-if="scope === 'document'"
            v-model="documentId"
            size="small"
            class="doc-select"
            placeholder="选择文档"
            @change="onDocumentChange"
          >
            <el-option
              v-for="doc in documents"
              :key="doc.id"
              :label="doc.filename"
              :value="doc.id"
            />
          </el-select>
          <el-select
            v-model="sourceFilter"
            clearable
            size="small"
            class="source-select"
            placeholder="全部来源"
            @change="applyFilters"
          >
            <el-option label="自动生成" value="auto" />
            <el-option label="手动添加" value="manual" />
          </el-select>
        </template>
        <template #actions v-if="kbId">
          <button class="refresh-btn" type="button" title="刷新" @click="load">
            <el-icon :class="{ spinning: loading }"><Refresh /></el-icon>
          </button>
          <el-button
            v-if="canWrite && generateDoc"
            size="small"
            plain
            :icon="'MagicStick'"
            :loading="generating"
            @click="generate"
          >
            本文档生成
          </el-button>
          <el-button
            v-if="canWrite"
            size="small"
            type="warning"
            plain
            :icon="'MagicStick'"
            :loading="batchGenerating"
            @click="generateBatch"
          >
            批量补齐
          </el-button>
          <el-button v-if="canWrite" size="small" type="primary" :icon="'Plus'" @click="openAdd">
            添加
          </el-button>
        </template>
      </FilterBar>
    </div>

    <div v-if="!kbId" class="faq-empty">
      <EmptyState title="先选择公司与知识库" text="选定知识库后，这里会列出库级与文档级 FAQ。" />
    </div>

    <DataPanel
      v-if="kbId"
      class="list-panel"
      title="FAQ 列表"
      :count="total > 0 ? `${total} 条` : null"
      v-loading="loading"
    >
      <div class="faq-items">
        <div
          v-for="entry in faqs"
          :key="entry.id"
          class="faq-item"
          :class="{ active: entry.id === selectedId }"
          @click="selectedId = entry.id"
        >
          <div class="faq-question">{{ entry.question }}</div>
          <div class="faq-meta">
            <StatusPill
              :label="sourceMeta[entry.source]?.label ?? entry.source"
              :tone="sourceMeta[entry.source]?.type ?? 'info'"
              :dot="false"
            />
            <span class="faq-doc">{{ docName(entry.document_id) }}</span>
          </div>
        </div>
        <EmptyState
          v-if="!loading && faqs.length === 0"
          :title="emptyTitle"
          :text="emptyHint"
          :muted="isFiltered"
        />
      </div>
      <div v-if="total > 0" class="list-pager">
        <el-pagination
          v-model:current-page="page"
          small
          background
          layout="total, prev, pager, next"
          :total="total"
          :page-size="pageSize"
          @current-change="load"
        />
      </div>
    </DataPanel>

    <DataPanel v-if="kbId" class="editor-panel" v-loading="loading">
      <template v-if="selected">
        <div class="editor-head">
          <div class="editor-question">
            <span class="q-mark">Q</span>
            <span>{{ selected.question }}</span>
          </div>
          <div class="editor-tags">
            <StatusPill
              :label="sourceMeta[selected.source]?.label ?? selected.source"
              :tone="sourceMeta[selected.source]?.type ?? 'info'"
              :dot="false"
            />
            <span class="editor-doc">{{ docName(selected.document_id) }}</span>
          </div>
        </div>
        <div class="answer-box">
          <div class="answer-label">A</div>
          <!-- 答案原文渲染为标准 Markdown，图片占位符不在此出现。 -->
          <div class="answer-content markdown-body" v-html="renderMarkdown(selected.answer)" />
        </div>
        <div class="editor-foot">
          <span class="foot-note">
            更新于 {{ formatDateTime(selected.updated_at) }}
          </span>
          <div class="foot-actions">
            <template v-if="canWrite">
              <el-button size="small" plain :icon="'MagicStick'" @click="openRewrite">
                AI 改写
              </el-button>
              <el-button size="small" plain :icon="'Edit'" @click="openEdit(selected)">
                编辑
              </el-button>
              <el-button size="small" plain type="danger" :icon="'Delete'" @click="remove(selected)">
                删除
              </el-button>
            </template>
          </div>
        </div>
      </template>
      <EmptyState
        v-else
        title="选择左侧条目查看详情"
        text="命中后原样直出的答案，可在此编辑或让 AI 改写。"
      />
    </DataPanel>

    <el-dialog
      v-model="dialogVisible"
      :title="editing ? '编辑 FAQ' : '添加 FAQ'"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-alert
        v-if="!editing && scope === 'document' && documentId"
        type="info"
        :closable="false"
        show-icon
        class="dialog-hint"
        :title="`将绑定到文档「${docName(documentId)}」，仅该文档范围内可命中`"
      />
      <el-form label-position="top">
        <el-form-item label="问题">
          <el-input
            v-model="form.question"
            type="textarea"
            :rows="2"
            maxlength="1000"
            placeholder="用户可能提出的问题，如：怎么开机？"
          />
        </el-form-item>
        <el-form-item label="答案">
          <el-input
            v-model="form.answer"
            type="textarea"
            :rows="6"
            maxlength="20000"
            show-word-limit
            placeholder="命中后原样直出的答案，支持 Markdown"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="rewriteVisible"
      title="AI 改写答案"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="dialog-hint"
        title="不填指令时自动润色：让答案更准确、完整、易读，不改变事实内容"
      />
      <el-input
        v-model="rewriteInstruction"
        type="textarea"
        :rows="3"
        maxlength="500"
        placeholder="可选：如「把步骤写成编号列表，语气更正式」"
      />
      <template #footer>
        <el-button @click="rewriteVisible = false">取消</el-button>
        <el-button type="primary" :loading="rewriting" :icon="'MagicStick'" @click="rewrite">
          开始改写
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.faq-panel {
  display: grid;
  grid-template-columns: 310px minmax(0, 1fr);
  grid-template-rows: auto minmax(0, 1fr);
  gap: 16px;
  min-width: 0;
  min-height: 420px;
}
.panel-bar {
  grid-column: 1 / -1;
}
/* 间距由网格 gap 控制，FilterBar 自带的 margin 会变成双倍。 */
.panel-bar :deep(.filter-bar) {
  margin-bottom: 0;
}
.faq-empty {
  grid-column: 1 / -1;
}
.search-input {
  width: 170px;
}
.scope-select {
  width: 100px;
}
.doc-select {
  width: 140px;
}
.source-select {
  width: 96px;
}
.refresh-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  background: var(--kb-panel-bg);
  color: var(--el-text-color-regular);
  cursor: pointer;
  transition:
    border-color 0.15s var(--kb-ease),
    color 0.15s var(--kb-ease);
}
.refresh-btn:hover {
  border-color: var(--el-color-primary-light-5);
  color: var(--el-color-primary);
}
.spinning {
  animation: spin 0.9s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
.list-panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.list-panel :deep(.panel-body) {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
}
.faq-items {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
  max-height: 640px;
  overflow-y: auto;
  padding-right: 2px;
}
.faq-item {
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 6px;
  transition:
    border-color 0.15s var(--kb-ease),
    background-color 0.15s var(--kb-ease);
}
.faq-item:hover {
  border-color: var(--el-border-color-dark);
  background: var(--el-fill-color-lighter);
}
.faq-item.active {
  border-color: var(--el-color-primary-light-5);
  background: var(--el-color-primary-light-9);
}
.faq-question {
  font-size: var(--kb-font-sm);
  font-weight: 600;
  color: var(--el-text-color-primary);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.faq-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}
.faq-doc {
  font-size: var(--kb-font-2xs);
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 150px;
}
.list-pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.editor-panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.editor-panel :deep(.panel-body) {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 12px;
}
.editor-panel :deep(.empty-state) {
  margin: auto;
}
.editor-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.editor-question {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: var(--kb-font-md);
  font-weight: 600;
  color: var(--el-text-color-primary);
  line-height: 1.5;
}
.q-mark,
.answer-label {
  flex: none;
  width: 22px;
  height: 22px;
  border-radius: 7px;
  display: grid;
  place-items: center;
  font-size: var(--kb-font-xs);
  font-weight: 700;
  color: #fff;
}
.q-mark {
  background: var(--el-color-primary);
  margin-top: 1px;
}
.answer-label {
  background: var(--el-color-success);
}
.editor-tags {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}
.editor-doc {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
}
.answer-box {
  display: flex;
  gap: 10px;
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-8);
  border-radius: var(--kb-panel-radius);
  padding: 14px;
}
.answer-label {
  margin-top: 1px;
}
.answer-content {
  flex: 1;
  font-size: var(--kb-font-md);
  line-height: 1.8;
  color: var(--el-text-color-regular);
  min-width: 0;
  overflow-x: auto;
}
.answer-content :deep(p) {
  margin: 0 0 8px;
}
.answer-content :deep(p:last-child) {
  margin-bottom: 0;
}
.answer-content :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
}
.answer-content :deep(th),
.answer-content :deep(td) {
  border: 1px solid var(--el-border-color);
  padding: 6px 10px;
  font-size: var(--kb-font-sm);
}
.editor-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: auto;
}
.foot-note {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
}
.foot-actions {
  display: flex;
  gap: 8px;
}
.dialog-hint {
  border-radius: var(--el-border-radius-small);
  margin-bottom: 12px;
}

@media (max-width: 900px) {
  .faq-panel {
    grid-template-columns: minmax(0, 1fr);
  }
  .faq-items {
    max-height: 360px;
  }
}
</style>
