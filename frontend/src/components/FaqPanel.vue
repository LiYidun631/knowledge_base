<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { client, errorMessage } from '../api/client'
import { renderMarkdown } from '../utils/markdown'

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

const props = defineProps<{ kbId: string }>()

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

const emptyText = computed(() => {
  if (q.value.trim() || sourceFilter.value) return '没有符合条件的 FAQ'
  if (scope.value === 'kb') return '暂无库级 FAQ，点击「添加」创建'
  if (scope.value === 'document' && !documentId.value) return '该知识库还没有文档'
  return '暂无 FAQ'
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
  await loadDocuments()
  await load()
})
</script>

<template>
  <div class="faq-panel">
    <div class="panel-bar">
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
        <span class="bar-spacer" />
        <el-button size="small" circle :icon="'Refresh'" title="刷新" @click="load" />
        <el-button size="small" type="primary" plain :icon="'Plus'" @click="openAdd">
          添加
        </el-button>
        <el-button
          v-if="generateDoc"
          size="small"
          type="success"
          plain
          :icon="'MagicStick'"
          :loading="generating"
          @click="generate"
        >
          本文档生成
        </el-button>
        <el-button
          size="small"
          type="warning"
          plain
          :icon="'MagicStick'"
          :loading="batchGenerating"
          @click="generateBatch"
        >
          批量补齐
        </el-button>
      </template>
    </div>

    <el-empty
      v-if="!kbId"
      class="faq-empty"
      description="先选择公司与知识库"
    />

    <div v-if="kbId" class="faq-list" v-loading="loading">
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
            <el-tag
              :type="sourceMeta[entry.source]?.type ?? 'info'"
              size="small"
              effect="plain"
            >
              {{ sourceMeta[entry.source]?.label ?? entry.source }}
            </el-tag>
            <span class="faq-doc">{{ docName(entry.document_id) }}</span>
          </div>
        </div>
        <el-empty
          v-if="!loading && faqs.length === 0"
          :description="emptyText"
          :image-size="72"
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
    </div>

    <div v-if="kbId" class="faq-editor" v-loading="loading">
      <template v-if="selected">
        <div class="editor-head">
          <div class="editor-question">
            <span class="q-mark">Q</span>
            <span>{{ selected.question }}</span>
          </div>
          <div class="editor-tags">
            <el-tag
              :type="sourceMeta[selected.source]?.type ?? 'info'"
              size="small"
              effect="plain"
            >
              {{ sourceMeta[selected.source]?.label ?? selected.source }}
            </el-tag>
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
            <el-button size="small" plain :icon="'MagicStick'" @click="openRewrite">
              AI 改写
            </el-button>
            <el-button size="small" plain :icon="'Edit'" @click="openEdit(selected)">
              编辑
            </el-button>
            <el-button size="small" plain type="danger" :icon="'Delete'" @click="remove(selected)">
              删除
            </el-button>
          </div>
        </div>
      </template>
      <el-empty v-else description="选择左侧条目查看详情" :image-size="72" />
    </div>

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
  grid-template-columns: 300px 1fr;
  grid-template-rows: auto 1fr;
  gap: 16px;
  min-height: 420px;
}
.panel-bar {
  grid-column: 1 / -1;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.search-input {
  width: 190px;
}
.scope-select {
  width: 110px;
}
.doc-select {
  width: 160px;
}
.source-select {
  width: 110px;
}
.faq-empty {
  grid-column: 1 / -1;
}
.bar-spacer {
  flex: 1;
}
.faq-list {
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
  padding: 12px;
  background: #fff;
  display: flex;
  flex-direction: column;
  max-height: 560px;
  overflow-y: auto;
}
.faq-items {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.faq-item {
  padding: 10px 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 6px;
  transition: border-color 0.15s var(--kb-ease), box-shadow 0.15s var(--kb-ease);
}
.faq-item:hover {
  border-color: var(--el-border-color-dark);
  box-shadow: 0 2px 8px rgba(15, 23, 42, 0.06);
}
.faq-item.active {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.faq-question {
  font-size: 13px;
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
  font-size: 11px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 150px;
}
.list-pager {
  margin-top: auto;
  padding-top: 8px;
  display: flex;
  justify-content: center;
}

.faq-editor {
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
  padding: 16px;
  background: #fff;
  display: flex;
  flex-direction: column;
  gap: 12px;
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
  font-size: 15px;
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
  font-size: 12px;
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
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.answer-box {
  display: flex;
  gap: 10px;
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-8);
  border-radius: var(--el-border-radius-base);
  padding: 14px;
}
.answer-label {
  margin-top: 1px;
}
.answer-content {
  flex: 1;
  font-size: 14px;
  line-height: 1.8;
  color: var(--el-text-color-regular);
  min-width: 0;
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
  font-size: 13px;
}
.editor-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: auto;
}
.foot-note {
  font-size: 12px;
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
    grid-template-columns: 1fr;
  }
}
</style>
