<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { client, errorMessage } from '../api/client'

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
  <el-card v-loading="loading">
    <template #header>
      <div class="head-row">
        <span class="head-title"><el-icon><EditPen /></el-icon>提示词管理</span>
        <span class="head-hint">保存后即时对在线问答与 FAQ 生成生效，重启服务不丢失</span>
      </div>
    </template>
    <div class="prompt-layout">
      <aside class="prompt-list">
        <div
          v-for="item in prompts"
          :key="item.name"
          class="prompt-item"
          :class="{ active: item.name === currentName }"
          @click="select(item.name)"
        >
          <div class="prompt-item-title">{{ item.label }}</div>
          <el-tag :type="sourceMeta[item.source]?.type ?? 'info'" size="small" effect="plain">
            {{ sourceMeta[item.source]?.label ?? item.source }}
          </el-tag>
        </div>
      </aside>
      <div v-if="current" class="prompt-editor">
        <div class="editor-head">
          <div class="editor-title">
            {{ current.label }}
            <el-tag :type="sourceMeta[current.source]?.type ?? 'info'" size="small" effect="plain" class="editor-tag">
              {{ sourceMeta[current.source]?.label ?? current.source }}
            </el-tag>
          </div>
          <div v-if="dirty" class="dirty-mark">有未保存修改</div>
        </div>
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
        <div class="editor-foot">
          <span class="foot-note">
            来源：{{ sourceMeta[current.source]?.label ?? current.source }}
            <template v-if="current.updated_at"> · 更新于 {{ current.updated_at.slice(0, 19).replace('T', ' ') }}</template>
          </span>
          <div class="foot-actions">
            <el-button v-if="current.source === 'override'" plain type="warning" @click="resetToDefault">
              回退默认
            </el-button>
            <el-button type="primary" :disabled="!dirty" :loading="saving" @click="save">
              保存覆写
            </el-button>
          </div>
        </div>
      </div>
      <el-empty v-else description="暂无可编辑提示词" />
    </div>
  </el-card>
</template>

<style scoped>
.head-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}
.head-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.head-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.prompt-layout {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: 20px;
  min-height: 420px;
}
.prompt-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.prompt-item {
  padding: 12px 14px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 8px;
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
  font-size: 14px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.prompt-editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.editor-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.editor-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.dirty-mark {
  font-size: 12px;
  color: var(--el-color-warning);
}
.editor-hint {
  border-radius: 6px;
}
.editor-input :deep(.el-textarea__inner) {
  font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace;
  font-size: 13px;
  line-height: 1.8;
}
.editor-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.foot-note {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.foot-actions {
  display: flex;
  gap: 10px;
}

@media (max-width: 900px) {
  .prompt-layout {
    grid-template-columns: 1fr;
  }
}
</style>
