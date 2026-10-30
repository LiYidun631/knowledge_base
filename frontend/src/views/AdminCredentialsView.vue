<script setup lang="ts">
import { nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { client, errorMessage } from '../api/client'
import { ROLE_LABELS, type AdminRole } from '../api/session'
import PageHeader from '../components/ui/PageHeader.vue'
import DataPanel from '../components/ui/DataPanel.vue'
import StatusPill from '../components/ui/StatusPill.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import { formatBeijingDate } from '../utils/datetime'

interface AdminCredential {
  id: string
  name: string
  role: AdminRole
  is_enabled: boolean
  username: string | null
  has_password: boolean
  created_at: string
  updated_at: string
}

const roleOptions: { value: AdminRole; label: string; hint: string }[] = [
  { value: 'admin', label: '管理员', hint: '全部权限，含系统设置与凭据管理' },
  { value: 'operator', label: '运营', hint: '可改业务数据，不能碰模型/提示词/凭据' },
  { value: 'viewer', label: '只读', hint: '只能查询与问答，不能写操作' },
]

const credentials = ref<AdminCredential[]>([])
const loading = ref(false)

const createDialog = reactive({
  visible: false,
  name: '',
  role: 'admin' as AdminRole,
  username: '',
  password: '',
  submitting: false,
})

// 账号密码只在弹窗内暂存，提交后不留内存副本。
const accountDialog = reactive({
  visible: false,
  id: '',
  name: '',
  username: '',
  password: '',
  hasPassword: false,
  hadUsername: false,
  submitting: false,
})

const createFormRef = ref<FormInstance>()
const accountFormRef = ref<FormInstance>()

// 规则与后端校验保持一致：账号 3–60 位（不区分大小写），密码 8–128 位。
const createRules: FormRules = {
  name: [{ required: true, whitespace: true, message: '请填写管理员标识', trigger: 'blur' }],
  username: [
    {
      trigger: 'blur',
      validator: (_rule, value: string, callback) => {
        const account = (value ?? '').trim()
        if (account && account.length < 3) callback(new Error('登录账号至少 3 个字符'))
        else callback()
      },
    },
  ],
  password: [
    {
      trigger: 'blur',
      validator: (_rule, value: string, callback) => {
        const password = value ?? ''
        const account = createDialog.username.trim()
        if (!password) {
          if (account) callback(new Error('填写登录账号时需同时设置密码'))
          else callback()
          return
        }
        if (!account) callback(new Error('设置密码前请先填写登录账号'))
        else if (password.length < 8) callback(new Error('登录密码至少 8 位'))
        else callback()
      },
    },
  ],
}

const accountRules: FormRules = {
  username: [
    {
      trigger: 'blur',
      validator: (_rule, value: string, callback) => {
        const account = (value ?? '').trim()
        if (account && account.length < 3) callback(new Error('登录账号至少 3 个字符'))
        else callback()
      },
    },
  ],
  password: [
    {
      trigger: 'blur',
      validator: (_rule, value: string, callback) => {
        const password = value ?? ''
        const account = accountDialog.username.trim()
        if (password && password.length < 8) callback(new Error('登录密码至少 8 位'))
        else if (password && !account) callback(new Error('设置密码前请先填写登录账号'))
        else if (account && !accountDialog.hasPassword && !password) {
          callback(new Error('首次开通账号需同时设置密码'))
        } else callback()
      },
    },
  ],
}

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
  createDialog.role = 'admin'
  createDialog.username = ''
  createDialog.password = ''
  createDialog.visible = true
  // 清掉上一次关闭时残留的红色提示。
  nextTick(() => createFormRef.value?.clearValidate())
}

async function createCredential() {
  // 校验失败时字段下方会显示红色说明，这里不再弹 toast。
  const valid = await createFormRef.value?.validate().catch(() => false)
  if (!valid) return
  const username = createDialog.username.trim()
  const password = createDialog.password
  createDialog.submitting = true
  try {
    const { data } = await client.post<{ admin_credential_id: string; token: string }>(
      '/admin/credentials',
      {
        name: createDialog.name.trim(),
        role: createDialog.role,
        ...(username ? { username, password } : {}),
      },
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

function openAccountDialog(credential: AdminCredential) {
  accountDialog.id = credential.id
  accountDialog.name = credential.name
  accountDialog.username = credential.username ?? ''
  accountDialog.password = ''
  accountDialog.hasPassword = credential.has_password
  accountDialog.hadUsername = Boolean(credential.username)
  accountDialog.visible = true
  nextTick(() => accountFormRef.value?.clearValidate())
}

async function saveAccount() {
  const valid = await accountFormRef.value?.validate().catch(() => false)
  if (!valid) return
  const username = accountDialog.username.trim()
  const password = accountDialog.password
  const payload: Record<string, string> = {}
  if (username) payload.username = username
  // 清空账号即收回账号密码登录入口。
  else if (accountDialog.hadUsername) payload.username = ''
  if (password) payload.password = password
  if (!Object.keys(payload).length) {
    ElMessage.info('没有需要保存的修改')
    return
  }
  accountDialog.submitting = true
  try {
    await client.patch(`/admin/credentials/${accountDialog.id}`, payload)
    accountDialog.visible = false
    ElMessage.success('已更新登录方式')
    await loadCredentials()
  } catch (error) {
    errorMessage(error, '更新登录方式失败')
  } finally {
    accountDialog.submitting = false
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

async function changeRole(credential: AdminCredential, role: AdminRole) {
  try {
    await client.patch(`/admin/credentials/${credential.id}`, { role })
    credential.role = role
    ElMessage.success(`已改为「${ROLE_LABELS[role]}」`)
  } catch (error) {
    errorMessage(error, '调整角色失败')
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
  // 库存 UTC，界面按北京时间展示。
  return formatBeijingDate(value)
}

onMounted(loadCredentials)
</script>

<template>
  <div class="admins-page">
    <PageHeader
      title="管理员管理"
      eyebrow="角色与凭据"
      subtitle="登录账号密码与长期 Token 都在此签发 · .env 引导 Token 始终有效，可作遗忘时的兜底入口"
    >
      <template #meta>
        <span class="header-count">共 {{ credentials.length }} 个</span>
      </template>
      <template #actions>
        <el-button type="primary" size="small" @click="openCreateDialog">
          <el-icon><Plus /></el-icon>新建管理员
        </el-button>
      </template>
    </PageHeader>

    <DataPanel flush>
      <el-table :data="credentials" v-loading="loading">
        <template #empty>
          <EmptyState
            title="还没有库内管理员凭据"
            text="新建管理员后会签发一枚明文 Token，交给对应的人登录管理端。"
          />
        </template>
        <el-table-column prop="name" label="管理员标识" min-width="150" show-overflow-tooltip />
        <el-table-column label="登录账号" width="150">
          <template #default="{ row }">
            <span v-if="row.username" class="account-name">{{ row.username }}</span>
            <StatusPill v-else label="仅 Token" tone="info" />
          </template>
        </el-table-column>
        <el-table-column label="角色" width="132">
          <template #default="{ row }">
            <el-select
              :model-value="row.role"
              size="small"
              @change="(role: AdminRole) => changeRole(row, role)"
            >
              <el-option
                v-for="option in roleOptions"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <StatusPill
              :label="row.is_enabled ? '启用' : '已停用'"
              :tone="row.is_enabled ? 'success' : 'info'"
            />
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
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openAccountDialog(row)">
              账号密码
            </el-button>
            <el-button size="small" text type="primary" @click="resetCredential(row)">
              重置
            </el-button>
            <el-button size="small" text type="danger" @click="removeCredential(row)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </DataPanel>

    <el-dialog
      v-model="createDialog.visible"
      title="新建管理员"
      width="440px"
      :close-on-click-modal="false"
    >
      <el-form
        ref="createFormRef"
        :model="createDialog"
        :rules="createRules"
        label-width="90px"
        class="credential-form"
      >
        <el-form-item label="管理员标识" prop="name" required>
          <el-input v-model="createDialog.name" maxlength="120" placeholder="例如 张三 / 值班账号" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="createDialog.role" style="width: 100%">
            <el-option
              v-for="option in roleOptions"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
          <div class="role-hint">
            {{ roleOptions.find((option) => option.value === createDialog.role)?.hint }}
          </div>
        </el-form-item>
        <el-form-item label="登录账号" prop="username">
          <el-input v-model="createDialog.username" maxlength="60" placeholder="选填，例如 liyidun" />
          <div class="field-hint">选填 · 3–60 个字符，不区分大小写，统一按小写保存</div>
        </el-form-item>
        <el-form-item label="登录密码" prop="password">
          <el-input
            v-model="createDialog.password"
            type="password"
            show-password
            maxlength="128"
            placeholder="填了账号就必须填，至少 8 位"
          />
          <div class="field-hint">8–128 个字符 · 只保存哈希，不回显</div>
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
      v-model="accountDialog.visible"
      :title="`「${accountDialog.name}」的登录账号密码`"
      width="440px"
      :close-on-click-modal="false"
    >
      <el-form
        ref="accountFormRef"
        :model="accountDialog"
        :rules="accountRules"
        label-width="90px"
        class="credential-form"
      >
        <el-form-item label="登录账号" prop="username">
          <el-input v-model="accountDialog.username" maxlength="60" placeholder="留空即收回账号登录" />
          <div class="field-hint">留空即收回账号登录 · 3–60 个字符，不区分大小写</div>
        </el-form-item>
        <el-form-item label="登录密码" prop="password">
          <el-input
            v-model="accountDialog.password"
            type="password"
            show-password
            maxlength="128"
            :placeholder="accountDialog.hasPassword ? '留空表示不修改' : '首次开通必须填写'"
          />
          <div class="field-hint">8–128 个字符 · 留空表示不修改</div>
        </el-form-item>
      </el-form>
      <p class="account-hint">
        会话 7 天有效；改密或停用凭据会把该管理员已登录的会话全部退出。长期 Token 不受影响。
      </p>
      <template #footer>
        <el-button @click="accountDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="accountDialog.submitting" @click="saveAccount">
          保存
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
  </div>
</template>

<style scoped>
.header-count {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}
.account-name {
  font-family: 'Cascadia Code', Consolas, 'Courier New', monospace;
  font-size: var(--kb-font-sm);
  color: var(--el-text-color-primary);
}
.account-hint {
  margin: 4px 0 0;
  font-size: var(--kb-font-xs);
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
.role-hint {
  margin-top: 6px;
  font-size: var(--kb-font-xs);
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}
.field-hint {
  margin-top: 4px;
  font-size: var(--kb-font-xs);
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}
/* 让红色校验文案占位显示在输入框与灰色规则说明之间，不与说明重叠。 */
.credential-form :deep(.el-form-item__error) {
  position: static;
  padding-top: 2px;
}
.token-alert {
  margin-bottom: 12px;
}
.token-box {
  display: flex;
  gap: 10px;
  align-items: center;
  background: var(--el-color-primary-light-9);
  border: 1px dashed var(--el-color-primary-light-7);
  border-radius: var(--el-border-radius-base);
  padding: 12px;
}
.token-text {
  flex: 1;
  word-break: break-all;
  font-size: var(--kb-font-sm);
  font-family: 'Cascadia Code', Consolas, 'Courier New', monospace;
  color: var(--el-color-primary-dark-2);
}
</style>
