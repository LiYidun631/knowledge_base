<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
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
}

interface ModelProfile {
  id: string
  name: string
  kind: 'embedding' | 'rerank' | 'chat'
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
  kind: 'embedding' | 'rerank' | 'chat'
  latency_ms: number
  message: string
  error_code: string | null
}

interface ProfileForm {
  name: string
  kind: 'embedding' | 'rerank' | 'chat'
  provider: string
  model_name: string
  dimensions: number | null
  base_url: string
  path: string
  batch_size: number
  secret_ref: string
  is_enabled: boolean
}

const activeTab = ref('profiles')
const presets = ref<ModelPreset[]>([])
const profiles = ref<ModelProfile[]>([])
const loading = ref(false)
const presetLoading = ref(false)

const dialogVisible = ref(false)
const editingId = ref<string | null>(null)
const submitting = ref(false)
const form = reactive<ProfileForm>(emptyForm())

const keyDialogVisible = ref(false)
const keyProfile = ref<ModelProfile | null>(null)
const apiKey = ref('')
const keySubmitting = ref(false)
const testingId = ref<string | null>(null)

const providerOptions = computed(() =>
  presets.value
    .filter((preset) =>
      form.kind === 'chat' ? preset.supports_chat : preset.supports_embedding,
    )
    .map((preset) => ({ value: preset.key, label: preset.display_name })),
)

const secretRefOptions = ['dashscope', 'bigmodel', 'volcengine', 'deepseek', 'qianfan'].map(
  (value) => ({ value, label: value }),
)

const modelNamePlaceholder = computed(() =>
  form.kind === 'chat'
    ? '供应商控制台中的生成模型名'
    : '供应商控制台中的模型名或 Endpoint ID',
)

const pathLabel = computed(() =>
  form.kind === 'chat' ? 'Chat 路径' : form.kind === 'rerank' ? 'Rerank 路径' : 'Embedding 路径',
)

function emptyForm(): ProfileForm {
  return {
    name: '',
    kind: 'embedding',
    provider: 'dashscope',
    model_name: '',
    dimensions: null,
    base_url: '',
    path: '/embeddings',
    batch_size: 1,
    secret_ref: '',
    is_enabled: true,
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

function applyPreset(provider: string) {
  const preset = presets.value.find((item) => item.key === provider)
  if (!preset) return
  form.base_url = preset.base_url
  if (form.kind === 'embedding') form.path = preset.embedding_path
  else if (form.kind === 'chat') form.path = preset.chat_path ?? '/chat/completions'
  else form.path = '/reranks'
}

function switchKind() {
  form.dimensions = null
  form.batch_size = 1
  const preset = presets.value.find((item) => item.key === form.provider)
  if (form.kind === 'embedding') {
    form.path = preset?.embedding_path ?? '/embeddings'
  } else if (form.kind === 'chat') {
    form.path = preset?.chat_path ?? '/chat/completions'
  } else {
    form.path = '/reranks'
  }
}

function openCreate() {
  Object.assign(form, emptyForm())
  editingId.value = null
  dialogVisible.value = true
}

function openEdit(profile: ModelProfile) {
  editingId.value = profile.id
  const settings = profile.settings ?? {}
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
          : String(settings.rerank_path ?? '/reranks'),
    batch_size: Number(settings.batch_size ?? 1),
    secret_ref: profile.secret_ref ?? '',
    is_enabled: profile.is_enabled,
  })
  dialogVisible.value = true
}

function buildPayload() {
  const settings: Record<string, unknown> = { base_url: form.base_url }
  if (form.kind === 'embedding') {
    settings.embedding_path = form.path
    settings.batch_size = form.batch_size
  } else if (form.kind === 'chat') {
    settings.chat_path = form.path
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
    if (editingId.value) {
      await client.patch(`/admin/model-profiles/${editingId.value}`, buildPayload())
      ElMessage.success('模型档案已更新')
    } else {
      await client.post('/admin/model-profiles', { ...buildPayload(), kind: form.kind })
      ElMessage.success('模型档案已创建')
    }
    dialogVisible.value = false
    await loadProfiles()
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
  } catch (error) {
    errorMessage(error, '切换启用状态失败')
    await loadProfiles()
  }
}

async function setDefault(profile: ModelProfile) {
  try {
    await client.post(`/admin/model-profiles/${profile.id}/default`)
    await loadProfiles()
  } catch (error) {
    errorMessage(error, '设为默认失败')
  }
}

async function clearDefault(profile: ModelProfile) {
  try {
    await client.delete(`/admin/model-profiles/${profile.id}/default`)
    await loadProfiles()
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
    await loadProfiles()
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

onMounted(() => {
  loadPresets()
  loadProfiles()
})
</script>

<template>
  <el-card>
    <template #header>
      <div class="header">
        <span class="header-title"><el-icon><Cpu /></el-icon>模型管理</span>
        <el-button @click="loadProfiles()">刷新</el-button>
      </div>
    </template>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="模型档案在大库创建时锁定；已被大库/子库绑定的档案不可修改或删除，换模型需新建大库。"
      class="tip"
    />

    <el-tabs v-model="activeTab">
      <el-tab-pane label="模型档案" name="profiles">
        <div class="toolbar">
          <el-button type="primary" @click="openCreate">新建档案</el-button>
        </div>
        <el-table v-loading="loading" :data="profiles" border>
          <el-table-column prop="name" label="名称" min-width="160" />
          <el-table-column label="类型" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.kind === 'embedding'" type="info">Embedding</el-tag>
              <el-tag v-else-if="row.kind === 'chat'" type="success">Chat 生成</el-tag>
              <el-tag v-else type="warning">Rerank</el-tag>
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
          <el-table-column prop="note" label="说明" min-width="180" />
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑模型档案' : '新建模型档案'"
      width="560px"
    >
      <el-form label-width="110px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="例如 aliyun-dashscope-embedding-v4" />
        </el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="form.kind" :disabled="Boolean(editingId)" @change="switchKind">
            <el-radio-button value="embedding">Embedding</el-radio-button>
            <el-radio-button value="rerank">Rerank</el-radio-button>
            <el-radio-button value="chat">Chat 生成</el-radio-button>
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
        <el-form-item v-if="form.kind === 'embedding'" label="批量大小">
          <el-input-number v-model="form.batch_size" :min="1" style="width: 100%" />
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
.key-hint {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
