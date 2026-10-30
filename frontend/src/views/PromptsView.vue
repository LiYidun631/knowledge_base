<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { client, errorMessage } from '../api/client'
import PageHeader from '../components/ui/PageHeader.vue'
import DataPanel from '../components/ui/DataPanel.vue'
import StatusPill from '../components/ui/StatusPill.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import { formatBeijingDateTime } from '../utils/datetime'

interface AdminPrompt {
  name: string
  label: string
  source: 'override' | 'file' | 'builtin'
  content: string
  updated_at: string | null
}

const prompts = ref<AdminPrompt[]>([])
const currentName = ref('')
const draft = ref('')
const dirty = ref(false)
const saving = ref(false)
const loading = ref(false)

const current = computed(() => prompts.value.find((item) => item.name === currentName.value) ?? null)

const sourceMeta: Record<string, { label: string; type: 'success' | 'info' | 'warning' }> = {
  override: { label: '管理页覆写', type: 'success' },
  file: { label: '提示词文件', type: 'info' },
  builtin: { label: '内置默认', type: 'warning' },
}

async function load() {
  loading.value = true
  try {
    const { data } = await client.get<AdminPrompt[]>('/admin/prompts')
    prompts.value = data
    if (!currentName.value && data.length > 0) currentName.value = data[0].name
    if (current.value) {
      draft.value = current.value.content
      dirty.value = false
    }
  } catch (error) {
    errorMessage(error, '加载提示词失败')
  } finally {
    loading.value = false
  }
}

function select(name: string) {
  if (dirty.value && current.value) {
    ElMessageBox.confirm('当前修改尚未保存，切换后将丢失。', '放弃修改？', {
      confirmButtonText: '放弃修改',
      cancelButtonText: '继续编辑',
      type: 'warning',
    })
      .then(() => {
        switchTo(name)
      })
      .catch(() => {})
    return
  }
  switchTo(name)
}

function switchTo(name: string) {
  currentName.value = name
  draft.value = current.value?.content ?? ''
  dirty.value = false
}

async function save() {
  const item = current.value
  if (!item || saving.value) return
  const content = draft.value.trim()
  if (!content) {
    ElMessage.warning('提示词正文不能为空')
    return
  }
  saving.value = true
  try {
    const { data } = await client.put<AdminPrompt>(`/admin/prompts/${item.name}`, { content })
    item.source = data.source
    item.content = data.content
    item.updated_at = data.updated_at
    draft.value = data.content
    dirty.value = false
    ElMessage.success('已保存，即时对在线问答生效')
  } catch (error) {
    errorMessage(error, '保存提示词失败')
  } finally {
    saving.value = false
  }
}

async function resetToDefault() {
  const item = current.value
  if (!item) return
  try {
    await ElMessageBox.confirm('将删除管理页覆写，提示词回落到文件或内置默认。', '回退提示词？', {
      confirmButtonText: '回退',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await client.delete(`/admin/prompts/${item.name}`)
    ElMessage.success('已回退默认')
    await load()
  } catch (error) {
    errorMessage(error, '回退提示词失败')
  }
}

onMounted(load)
</script>

<template>
  <div class="prompts-page">
    <PageHeader
      title="提示词管理"
      eyebrow="生成口径"
      subtitle="保存后即时对在线问答与 FAQ 生成生效 · 覆写存在库内，重启服务不丢失"
    >
      <template #meta>
        <span class="header-count">共 {{ prompts.length }} 个</span>
        <span v-if="dirty" class="meta-dirty">未保存</span>
      </template>
      <template #actions>
        <el-button size="small" :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>刷新
        </el-button>
      </template>
    </PageHeader>

    <div class="prompt-layout">
      <DataPanel
        class="list-panel"
        title="提示词"
        :count="prompts.length ? `${prompts.length} 个` : null"
        v-loading="loading"
      >
        <div class="prompt-items">
          <div
            v-for="item in prompts"
            :key="item.name"
            class="prompt-item"
            :class="{ active: item.name === currentName }"
            @click="select(item.name)"
          >
            <div class="prompt-item-title">{{ item.label }}</div>
            <StatusPill
              :label="sourceMeta[item.source]?.label ?? item.source"
              :tone="sourceMeta[item.source]?.type ?? 'info'"
            />
          </div>
          <EmptyState
            v-if="!loading && prompts.length === 0"
            title="没有可编辑的提示词"
            text="接口未返回提示词，刷新后仍为空请检查服务日志。"
          />
        </div>
      </DataPanel>

      <DataPanel
        class="editor-panel"
        :title="current?.label"
        v-loading="loading"
      >
        <template #actions v-if="current">
          <StatusPill
            :label="sourceMeta[current.source]?.label ?? current.source"
            :tone="sourceMeta[current.source]?.type ?? 'info'"
            :dot="false"
          />
        </template>

        <template v-if="current">
          <el-alert
            v-if="current.name === 'knowledge_answer_system'"
            type="info"
            :closable="false"
            show-icon
            class="editor-hint"
            title="提示词中的 {{brand_name}} 占位符会在生成时替换为 .env 配置的品牌名"
          />
          <el-input
            v-model="draft"
            type="textarea"
            :rows="16"
            maxlength="20000"
            show-word-limit
            class="editor-input"
            @input="dirty = true"
          />
        </template>
        <EmptyState
          v-else-if="!loading"
          title="暂无可编辑提示词"
          text="选择左侧提示词即可查看与覆写正文。"
        />

        <template v-if="current" #footer>
          <div class="editor-foot">
            <span class="foot-note">
              来源：{{ sourceMeta[current.source]?.label ?? current.source }}
              <template v-if="current.updated_at">
                · 更新于 {{ formatBeijingDateTime(current.updated_at) }}
              </template>
            </span>
            <div class="foot-actions">
              <el-button
                v-if="current.source === 'override'"
                size="small"
                plain
                type="warning"
                @click="resetToDefault"
              >
                回退默认
              </el-button>
              <el-button size="small" type="primary" :disabled="!dirty" :loading="saving" @click="save">
                保存覆写
              </el-button>
            </div>
          </div>
        </template>
      </DataPanel>
    </div>
  </div>
</template>

<style scoped>
.header-count {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}
.meta-dirty {
  font-size: var(--kb-font-xs);
  color: var(--el-color-warning);
}

.prompt-layout {
  display: grid;
  grid-template-columns: 230px minmax(0, 1fr);
  gap: 16px;
  min-width: 0;
}
.list-panel,
.editor-panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.list-panel :deep(.panel-body) {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
}
.prompt-items {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 8px;
}
.prompt-item {
  padding: 11px 13px;
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 7px;
  transition: border-color 0.15s var(--kb-ease), box-shadow 0.15s var(--kb-ease);
}
.prompt-item:hover {
  border-color: var(--el-border-color-dark);
  box-shadow: 0 2px 8px rgba(15, 23, 42, 0.06);
}
.prompt-item.active {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.prompt-item-title {
  font-size: var(--kb-font-sm);
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.editor-panel :deep(.panel-body) {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 12px;
}
.editor-hint {
  border-radius: var(--el-border-radius-small);
}
.editor-input {
  width: 100%;
}
.editor-input :deep(.el-textarea__inner) {
  font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace;
  font-size: var(--kb-font-sm);
  line-height: 1.8;
}
.editor-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}
.foot-note {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
}
.foot-actions {
  display: flex;
  gap: 8px;
  flex: none;
}

@media (max-width: 900px) {
  .prompt-layout {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
