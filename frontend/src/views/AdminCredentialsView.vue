<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { client, errorMessage } from '../api/client'

interface AdminCredential {
  id: string
  name: string
  is_enabled: boolean
  created_at: string
  updated_at: string
}

const credentials = ref<AdminCredential[]>([])
const loading = ref(false)

const createDialog = reactive({
  visible: false,
  name: '',
  submitting: false,
})

// 明文 Token 只在内存中保留到弹窗关闭，不写入任何存储或日志。
const tokenDialog = reactive({
  visible: false,
  name: '',
  token: '',
  copied: false,
})

async function loadCredentials() {
  loading.value = true
  try {
    const { data } = await client.get<AdminCredential[]>('/admin/credentials')
    credentials.value = data
  } catch (error) {
    errorMessage(error, '加载管理员凭据失败')
  } finally {
    loading.value = false
  }
}

function openCreateDialog() {
  createDialog.name = ''
  createDialog.visible = true
}

async function createCredential() {
  if (!createDialog.name.trim()) {
    ElMessage.warning('请填写管理员标识')
    return
  }
  createDialog.submitting = true
  try {
    const { data } = await client.post<{ admin_credential_id: string; token: string }>(
      '/admin/credentials',
      { name: createDialog.name.trim() },
    )
    createDialog.visible = false
    ElMessage.success('管理员凭据已创建')
    showToken(createDialog.name.trim(), data.token)
    await loadCredentials()
  } catch (error) {
    errorMessage(error, '创建管理员凭据失败')
  } finally {
    createDialog.submitting = false
  }
}

function showToken(name: string, token: string) {
  tokenDialog.name = name
  tokenDialog.token = token
  tokenDialog.copied = false
  tokenDialog.visible = true
}

async function copyToken() {
  try {
    await navigator.clipboard.writeText(tokenDialog.token)
    tokenDialog.copied = true
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.warning('复制失败，请手动选择复制')
  }
}

async function resetCredential(credential: AdminCredential) {
  try {
    await ElMessageBox.confirm(
      `重置「${credential.name}」的 Token？旧 Token 会立即失效，新 Token 明文仅本次显示。`,
      '重置管理员 Token',
      { type: 'warning', confirmButtonText: '重置', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const { data } = await client.post<{ token: string }>(
      `/admin/credentials/${credential.id}/reset`,
    )
    showToken(credential.name, data.token)
    await loadCredentials()
  } catch (error) {
    errorMessage(error, '重置管理员 Token 失败')
  }
}

async function toggleEnabled(credential: AdminCredential, value: string | number | boolean) {
  try {
    await client.patch(`/admin/credentials/${credential.id}`, { is_enabled: Boolean(value) })
    credential.is_enabled = Boolean(value)
  } catch (error) {
    errorMessage(error, '切换启用状态失败')
    await loadCredentials()
  }
}

async function removeCredential(credential: AdminCredential) {
  try {
    await ElMessageBox.confirm(
      `删除管理员凭据「${credential.name}」？该 Token 对新请求立即失效。`,
      '删除管理员凭据',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/admin/credentials/${credential.id}`)
    ElMessage.success('已删除')
    await loadCredentials()
  } catch (error) {
    errorMessage(error, '删除管理员凭据失败')
  }
}

function formatDate(value: string): string {
  return value.slice(0, 10)
}

onMounted(loadCredentials)
</script>

<template>
  <el-card>
    <template #header>
      <div class="head-row">
        <span class="head-title"><el-icon><User /></el-icon>管理员管理</span>
        <el-button type="primary" @click="openCreateDialog">新建管理员</el-button>
      </div>
    </template>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="库内管理员 Token 由这里签发；.env 中的引导 Token 始终有效，可作为遗忘凭据时的兜底入口。"
      class="tip"
    />

    <el-table :data="credentials" v-loading="loading" empty-text="暂无库内管理员凭据">
      <el-table-column prop="name" label="管理员标识" min-width="160" show-overflow-tooltip />
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.is_enabled" type="success" size="small">启用</el-tag>
          <el-tag v-else type="info" size="small">已停用</el-tag>
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
      <el-table-column label="创建时间" width="110">
        <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="resetCredential(row)">
            重置
          </el-button>
          <el-button size="small" text type="danger" @click="removeCredential(row)">
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog
      v-model="createDialog.visible"
      title="新建管理员"
      width="440px"
      :close-on-click-modal="false"
    >
      <el-form label-width="90px">
        <el-form-item label="管理员标识" required>
          <el-input v-model="createDialog.name" maxlength="120" placeholder="例如 张三 / 值班账号" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="createDialog.submitting" @click="createCredential">
          创建并签发 Token
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="tokenDialog.visible"
      :title="`「${tokenDialog.name}」的管理员 Token`"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="明文 Token 只显示这一次，关闭后无法再次查看，请立即安全保存。"
        class="token-alert"
      />
      <div class="token-box">
        <code class="token-text">{{ tokenDialog.token }}</code>
        <el-button size="small" type="primary" plain @click="copyToken">
          {{ tokenDialog.copied ? '已复制' : '复制' }}
        </el-button>
      </div>
      <template #footer>
        <el-button type="primary" @click="tokenDialog.visible = false">我已保存</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.head-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--el-color-primary);
}
.tip {
  margin-bottom: 12px;
}
.token-alert {
  margin-bottom: 12px;
}
.token-box {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--el-color-primary-light-9);
  border: 1px dashed var(--el-color-primary-light-7);
  border-radius: var(--el-border-radius-base);
  padding: 12px;
}
.token-text {
  flex: 1;
  word-break: break-all;
  font-size: 13px;
  font-family: 'Cascadia Code', Consolas, 'Courier New', monospace;
  color: var(--el-color-primary-dark-2);
}
</style>
