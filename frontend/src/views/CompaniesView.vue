<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { client, errorMessage } from '../api/client'
import { canWrite, isFullAdmin } from '../api/session'
import PageHeader from '../components/ui/PageHeader.vue'
import DataPanel from '../components/ui/DataPanel.vue'
import StatusPill from '../components/ui/StatusPill.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import { formatBeijingDate } from '../utils/datetime'

interface Company {
  id: string
  code: string
  name: string
  description: string | null
  status: string
  credential_count: number
  active_credential_count: number
  has_vector_library: boolean
  created_at: string
  updated_at: string
}

interface CompanyCredential {
  id: string
  company_id: string
  name: string
  is_enabled: boolean
  created_at: string
  updated_at: string
}

const companies = ref<Company[]>([])
const loading = ref(false)

const createDialog = reactive({
  visible: false,
  name: '',
  description: '',
  submitting: false,
})

const credentialDialog = reactive({
  visible: false,
  company: null as Company | null,
  items: [] as CompanyCredential[],
  loading: false,
  name: '',
  submitting: false,
  busyId: '',
})

const isArchived = computed(() => credentialDialog.company?.status !== 'active')

// 明文 Token 只在内存中保留到弹窗关闭，不写入任何存储或日志。
const tokenDialog = reactive({
  visible: false,
  companyName: '',
  credentialName: '',
  token: '',
  copied: false,
})

async function loadCompanies() {
  loading.value = true
  try {
    const { data } = await client.get<Company[]>('/companies')
    companies.value = data
  } catch (error) {
    errorMessage(error, '加载公司列表失败')
  } finally {
    loading.value = false
  }
}

async function loadCredentials() {
  if (!credentialDialog.company) return
  credentialDialog.loading = true
  try {
    const { data } = await client.get<CompanyCredential[]>(
      `/companies/${credentialDialog.company.id}/tokens`,
    )
    credentialDialog.items = data
  } catch (error) {
    errorMessage(error, '加载凭据列表失败')
  } finally {
    credentialDialog.loading = false
  }
}

async function refreshCredentialData() {
  const companyId = credentialDialog.company?.id
  await Promise.all([loadCredentials(), loadCompanies()])
  if (companyId) {
    credentialDialog.company =
      companies.value.find((company) => company.id === companyId) ?? credentialDialog.company
  }
}

function openCreateDialog() {
  createDialog.name = ''
  createDialog.description = ''
  createDialog.visible = true
}

async function createCompany() {
  if (!createDialog.name.trim()) {
    ElMessage.warning('请填写公司名称')
    return
  }
  createDialog.submitting = true
  try {
    await client.post('/companies', {
      name: createDialog.name.trim(),
      description: createDialog.description.trim() || null,
    })
    createDialog.visible = false
    ElMessage.success('公司已创建')
    await loadCompanies()
  } catch (error) {
    errorMessage(error, '创建公司失败')
  } finally {
    createDialog.submitting = false
  }
}

function openCredentialDialog(company: Company) {
  credentialDialog.company = company
  credentialDialog.items = []
  credentialDialog.name = ''
  credentialDialog.busyId = ''
  credentialDialog.visible = true
  void loadCredentials()
}

function showToken(company: Company, credentialName: string, token: string) {
  tokenDialog.companyName = company.name
  tokenDialog.credentialName = credentialName
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

async function issueCredential() {
  const company = credentialDialog.company
  if (!company) return
  const name = credentialDialog.name.trim()
  if (!name) {
    ElMessage.warning('请填写项目标识')
    return
  }
  credentialDialog.submitting = true
  try {
    const { data } = await client.post<{
      company_credential_id: string
      name: string
      token: string
    }>(`/companies/${company.id}/tokens`, { name })
    credentialDialog.name = ''
    showToken(company, data.name, data.token)
    await refreshCredentialData()
  } catch (error) {
    errorMessage(error, '签发凭据失败')
  } finally {
    credentialDialog.submitting = false
  }
}

async function resetCredential(credential: CompanyCredential) {
  const company = credentialDialog.company
  if (!company) return
  try {
    await ElMessageBox.confirm(
      `重置「${credential.name}」的 Token？仅该项目的旧 Token 立即失效，` +
        '同公司其他项目不受影响；新 Token 明文仅本次显示。',
      '重置凭据',
      { type: 'warning', confirmButtonText: '重置', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  credentialDialog.busyId = credential.id
  try {
    const { data } = await client.post<{ name: string; token: string }>(
      `/companies/${company.id}/tokens/${credential.id}/reset`,
    )
    showToken(company, data.name, data.token)
    await refreshCredentialData()
  } catch (error) {
    errorMessage(error, '重置凭据失败')
  } finally {
    credentialDialog.busyId = ''
  }
}

async function disableCredential(credential: CompanyCredential) {
  const company = credentialDialog.company
  if (!company) return
  try {
    await ElMessageBox.confirm(
      `停用「${credential.name}」的凭据？仅该项目的 Token 立即失效，` +
        '同公司其他项目不受影响。',
      '停用凭据',
      { type: 'warning', confirmButtonText: '停用', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  credentialDialog.busyId = credential.id
  try {
    await client.delete(`/companies/${company.id}/tokens/${credential.id}`)
    ElMessage.success('凭据已停用')
    await refreshCredentialData()
  } catch (error) {
    errorMessage(error, '停用凭据失败')
  } finally {
    credentialDialog.busyId = ''
  }
}

async function deleteVectorLibrary(company: Company) {
  try {
    await ElMessageBox.confirm(
      `删除「${company.name}」的向量大库？仅在大库下没有任何子库时允许删除，删除后不可恢复。`,
      '删除向量大库',
      { type: 'error', confirmButtonText: '删除大库', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/companies/${company.id}/vector-library`)
    ElMessage.success('向量大库已删除')
    await loadCompanies()
  } catch (error) {
    errorMessage(error, '删除向量大库失败')
  }
}

async function removeCompany(company: Company) {
  try {
    await ElMessageBox.confirm(
      `删除公司「${company.name}」？删除后不可恢复。公司下仍有向量大库时会被拒绝，` +
        '需先在知识库管理页删除全部子库，再在本页删除向量大库。',
      '删除公司',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/companies/${company.id}`)
    ElMessage.success('公司已删除')
    await loadCompanies()
  } catch (error) {
    errorMessage(error, '删除公司失败')
  }
}

function formatDate(value: string): string {
  // 库存 UTC，界面按北京时间展示。
  return formatBeijingDate(value)
}

onMounted(loadCompanies)
</script>

<template>
  <div class="companies-page">
    <PageHeader
      title="公司管理"
      eyebrow="租户与凭据"
      subtitle="公司是知识库与访问凭据的归属单位 · 每枚凭据对应一个接入项目"
    >
      <template #meta>
        <span class="header-count">共 {{ companies.length }} 家</span>
      </template>
      <template #actions>
        <el-button v-if="canWrite" type="primary" size="small" @click="openCreateDialog">
          <el-icon><Plus /></el-icon>新建公司
        </el-button>
      </template>
    </PageHeader>

    <DataPanel flush>
      <el-table :data="companies" v-loading="loading">
        <template #empty>
          <EmptyState
            title="还没有公司"
            :text="canWrite ? '新建公司后即可创建知识库、签发接入凭据。' : '等待管理员创建公司。'"
          />
        </template>
        <el-table-column prop="name" label="公司名称" min-width="160" show-overflow-tooltip />
        <el-table-column prop="code" label="编码" width="140" show-overflow-tooltip />
        <el-table-column label="说明" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.description || '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <StatusPill
              :label="row.status === 'active' ? '启用' : row.status === 'archived' ? '已归档' : row.status"
              :tone="row.status === 'active' ? 'success' : 'info'"
            />
          </template>
        </el-table-column>
        <el-table-column label="访问凭据" width="130">
          <template #default="{ row }">
            <span v-if="row.credential_count === 0" class="cell-muted">未签发</span>
            <StatusPill
              v-else
              :label="`${row.active_credential_count} / ${row.credential_count} 启用`"
              :tone="row.active_credential_count > 0 ? 'success' : 'warning'"
              :dot="false"
            />
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="110">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="300" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="isFullAdmin"
              size="small"
              text
              type="primary"
              @click="openCredentialDialog(row)"
            >
              凭据管理
            </el-button>
            <el-button
              v-if="canWrite && row.has_vector_library"
              size="small"
              text
              type="warning"
              @click="deleteVectorLibrary(row)"
            >
              删除大库
            </el-button>
            <el-button v-if="canWrite" size="small" text type="danger" @click="removeCompany(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </DataPanel>

    <el-dialog
      v-model="createDialog.visible"
      title="新建公司"
      width="440px"
      :close-on-click-modal="false"
    >
      <el-form label-width="80px">
        <el-form-item label="公司名称" required>
          <el-input v-model="createDialog.name" maxlength="160" placeholder="同一名称不能重复" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="createDialog.description" type="textarea" :rows="2" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="createDialog.submitting" @click="createCompany">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="credentialDialog.visible"
      :title="`「${credentialDialog.company?.name ?? ''}」的访问凭据`"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-alert
        v-if="isArchived"
        type="info"
        :closable="false"
        show-icon
        title="公司已归档，只能停用现有凭据，不能签发或重置。"
        class="credential-alert"
      />
      <div class="credential-add">
        <el-input
          v-model="credentialDialog.name"
          maxlength="120"
          placeholder="项目标识，例如：问诊助手"
          :disabled="isArchived || credentialDialog.submitting"
          @keyup.enter="issueCredential"
        />
        <el-button
          type="primary"
          :loading="credentialDialog.submitting"
          :disabled="isArchived"
          @click="issueCredential"
        >
          签发新凭据
        </el-button>
      </div>
      <el-table
        :data="credentialDialog.items"
        v-loading="credentialDialog.loading"
        empty-text="尚未签发凭据"
        size="small"
      >
        <el-table-column prop="name" label="项目" min-width="180" show-overflow-tooltip />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <StatusPill
              :label="row.is_enabled ? '启用中' : '已停用'"
              :tone="row.is_enabled ? 'success' : 'info'"
            />
          </template>
        </el-table-column>
        <el-table-column label="签发时间" width="110">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180">
          <template #default="{ row }">
            <el-button
              size="small"
              text
              type="primary"
              :disabled="isArchived || Boolean(credentialDialog.busyId)"
              @click="resetCredential(row)"
            >
              {{ row.is_enabled ? '重置' : '重置并启用' }}
            </el-button>
            <el-button
              v-if="row.is_enabled"
              size="small"
              text
              type="danger"
              :loading="credentialDialog.busyId === row.id"
              :disabled="Boolean(credentialDialog.busyId)"
              @click="disableCredential(row)"
            >
              停用
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <p class="credential-hint">
        每枚凭据对应一个项目，可独立重置或停用；同一公司下的凭据共享全部知识库权限。
      </p>
      <template #footer>
        <el-button type="primary" @click="credentialDialog.visible = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="tokenDialog.visible"
      :title="`「${tokenDialog.companyName} / ${tokenDialog.credentialName}」的访问 Token`"
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
.cell-muted {
  color: var(--el-text-color-placeholder);
  font-size: var(--kb-font-xs);
}
.credential-alert {
  margin-bottom: 12px;
}
.credential-add {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
}
.credential-hint {
  margin: 12px 0 0;
  color: var(--el-text-color-secondary);
  font-size: var(--kb-font-xs);
  line-height: 1.6;
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
