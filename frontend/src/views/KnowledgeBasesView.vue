<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import axios from 'axios'
import { ElMessage, ElMessageBox } from 'element-plus'
import { client, errorMessage } from '../api/client'
import { canWrite, isFullAdmin } from '../api/session'
import PageHeader from '../components/ui/PageHeader.vue'
import DataPanel from '../components/ui/DataPanel.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import StatusPill from '../components/ui/StatusPill.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import { formatBeijingDate } from '../utils/datetime'

interface Company {
  id: string
  code: string
  name: string
  description: string | null
  status: string
  created_at: string
  updated_at: string
}

interface ModelProfile {
  id: string
  name: string
  kind: 'embedding' | 'rerank'
  model_name: string
  is_enabled: boolean
  is_default: boolean
}

interface KnowledgeBase {
  id: string
  company_id: string | null
  name: string
  description: string | null
  embedding_model_id: string
  rerank_model_id: string | null
  chunk_parent_chars: number | null
  chunk_child_chars: number | null
  chunk_child_overlap_chars: number | null
  status: string
  archived_at: string | null
  created_at: string
  updated_at: string
}

interface ChunkDefaults {
  parent_chunk_chars: number
  child_chunk_chars: number
  child_chunk_overlap_chars: number
}

const route = useRoute()
const router = useRouter()
const loading = ref(false)
const companies = ref<Company[]>([])
const profiles = ref<ModelProfile[]>([])
const kbs = ref<KnowledgeBase[]>([])
const selectedCompanyId = ref('')
const showArchived = ref(false)
const chunkDefaults = ref<ChunkDefaults | null>(null)

const activeCompanies = computed(() => companies.value.filter((c) => c.status === 'active'))
const activeKbs = computed(() => kbs.value.filter((kb) => kb.status === 'active'))
const archivedKbs = computed(() => kbs.value.filter((kb) => kb.status !== 'active'))
const embeddingProfiles = computed(() =>
  profiles.value.filter((p) => p.kind === 'embedding' && p.is_enabled),
)
const rerankProfiles = computed(() => profiles.value.filter((p) => p.kind === 'rerank' && p.is_enabled))

// 顶栏搜索通过 q 参数进入本页，前端按名称与说明过滤。
const searchKeyword = computed(() =>
  typeof route.query.q === 'string' ? route.query.q.trim() : '',
)
const filteredActiveKbs = computed(() => filterKbs(activeKbs.value))
const filteredArchivedKbs = computed(() => filterKbs(archivedKbs.value))
// 页头 meta 用：搜索时显示命中数，否则显示启用中数量。
const resultCount = computed(
  () => filteredActiveKbs.value.length + (showArchived.value ? filteredArchivedKbs.value.length : 0),
)

function filterKbs(list: KnowledgeBase[]): KnowledgeBase[] {
  const keyword = searchKeyword.value.toLowerCase()
  if (!keyword) return list
  return list.filter(
    (kb) =>
      kb.name.toLowerCase().includes(keyword) ||
      (kb.description ?? '').toLowerCase().includes(keyword),
  )
}

function clearKeyword() {
  const query = { ...route.query }
  delete query.q
  router.replace({ path: route.path, query })
}

function profileName(id: string | null): string {
  if (!id) return '未启用'
  return profiles.value.find((p) => p.id === id)?.name ?? '未知模型'
}

function chunkSummary(kb: KnowledgeBase): string {
  const { chunk_parent_chars: p, chunk_child_chars: c, chunk_child_overlap_chars: o } = kb
  if (p === null && c === null && o === null) return '分块参数跟随全局默认'
  const part = (label: string, value: number | null) =>
    value === null ? null : `${label} ${value}`
  return [part('父块', p), part('子块', c), part('重叠', o)].filter(Boolean).join(' · ')
}

function formatDate(value: string): string {
  // 库存 UTC，界面按北京时间展示。
  return formatBeijingDate(value)
}

async function loadKbs() {
  if (!selectedCompanyId.value) return
  loading.value = true
  try {
    const { data } = await client.get<KnowledgeBase[]>('/knowledge-bases', {
      params: {
        company_id: selectedCompanyId.value,
        limit: 200,
        ...(showArchived.value ? { include_archived: true } : {}),
      },
    })
    kbs.value = data
  } catch (error) {
    errorMessage(error, '加载知识库列表失败')
  } finally {
    loading.value = false
  }
}

async function restoreKb(kb: KnowledgeBase) {
  try {
    await ElMessageBox.confirm(
      `恢复知识库「${kb.name}」？恢复后立即参与检索。`,
      '恢复确认',
      { type: 'info', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.post(`/knowledge-bases/${kb.id}/restore`)
    ElMessage.success('已恢复')
    await loadKbs()
  } catch (error) {
    errorMessage(error, '恢复失败')
  }
}

async function archiveKb(kb: KnowledgeBase) {
  try {
    await ElMessageBox.confirm(
      `归档知识库「${kb.name}」？文件与向量会保留，但不再参与检索。`,
      '归档确认',
      { type: 'warning', confirmButtonText: '归档', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/knowledge-bases/${kb.id}`)
    ElMessage.success('已归档')
    await loadKbs()
  } catch (error) {
    errorMessage(error, '归档失败')
  }
}

// 彻底删除复用公司侧的子库删除接口：文件、切块、BM25 与向量一并清理，不可恢复。
// 只有已归档的知识库才提供入口，避免误删仍在检索的库。
async function purgeKb(kb: KnowledgeBase) {
  if (!kb.company_id) {
    ElMessage.warning('该知识库缺少公司归属，无法删除')
    return
  }
  try {
    await ElMessageBox.confirm(
      `彻底删除知识库「${kb.name}」？其全部文件、切块与向量都会被删除，且不可恢复。`,
      '彻底删除确认',
      {
        type: 'error',
        confirmButtonText: '彻底删除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/companies/${kb.company_id}/vector-library/knowledge-bases/${kb.id}`)
    ElMessage.success('知识库已彻底删除')
    await loadKbs()
  } catch (error) {
    errorMessage(error, '删除知识库失败')
  }
}

// —— 新建向导 ——

const wizard = reactive({
  visible: false,
  step: 0,
  companyId: '',
  needLibrary: false,
  checked: false,
  libraryName: '',
  embeddingModelId: '',
  rerankChoice: '' as string,
  name: '',
  description: '',
  parentChars: null as number | null,
  childChars: null as number | null,
  overlapChars: null as number | null,
  submitting: false,
})

const wizardSteps = computed(() =>
  wizard.needLibrary ? ['选择公司', '模型与大库', '子库信息'] : ['选择公司', '子库信息'],
)
const selectedCompany = computed(
  () => activeCompanies.value.find((c) => c.id === wizard.companyId) ?? null,
)

function openWizard() {
  if (!canWrite.value) return
  wizard.visible = true
  wizard.step = 0
  wizard.companyId = activeCompanies.value.some((c) => c.id === selectedCompanyId.value)
    ? selectedCompanyId.value
    : (activeCompanies.value[0]?.id ?? '')
  wizard.needLibrary = false
  wizard.checked = false
  wizard.libraryName = ''
  wizard.embeddingModelId =
    embeddingProfiles.value.find((p) => p.is_default)?.id ?? embeddingProfiles.value[0]?.id ?? ''
  wizard.rerankChoice = ''
  wizard.name = ''
  wizard.description = ''
  wizard.parentChars = chunkDefaults.value?.parent_chunk_chars ?? null
  wizard.childChars = chunkDefaults.value?.child_chunk_chars ?? null
  wizard.overlapChars = chunkDefaults.value?.child_chunk_overlap_chars ?? null
  wizard.submitting = false
  // 消费掉顶栏「新建知识库」带来的 create 参数，保证再次点击仍能触发。
  if (route.query.create) {
    const query = { ...route.query }
    delete query.create
    router.replace({ path: route.path, query })
  }
}

// 顶栏按钮跳转 /kbs?create=1；公司数据可能尚未加载，两者就绪后再打开向导。
watch(
  [() => route.query.create, () => activeCompanies.value.length],
  ([create, count]) => {
    if (create === '1' && count > 0 && !wizard.visible) openWizard()
  },
)

async function checkLibrary(): Promise<boolean> {
  try {
    await client.get(`/companies/${wizard.companyId}/vector-library`)
    wizard.needLibrary = false
    wizard.checked = true
    return true
  } catch (error) {
    if (axios.isAxiosError(error) && error.response?.status === 404) {
      wizard.needLibrary = true
      wizard.checked = true
      return true
    }
    errorMessage(error, '检查大库状态失败')
    return false
  }
}

async function onNext() {
  if (wizard.step === 0) {
    if (!(await checkLibrary())) return
    if (wizard.needLibrary) {
      wizard.libraryName = `${selectedCompany.value?.name ?? ''}向量大库`
    }
  }
  wizard.step += 1
}

function onPrev() {
  wizard.step -= 1
}

async function onCreate() {
  if (!wizard.name.trim()) {
    ElMessage.warning('请填写子库名称')
    return
  }
  if (wizard.needLibrary && !isFullAdmin.value) {
    ElMessage.warning('公司还没有向量大库，需要管理员先创建')
    return
  }
  wizard.submitting = true
  try {
    if (wizard.needLibrary) {
      await client.post(`/companies/${wizard.companyId}/vector-library`, {
        name: wizard.libraryName.trim() || `${selectedCompany.value?.name ?? '公司'}向量大库`,
        embedding_model_id: wizard.embeddingModelId || undefined,
        ...(wizard.rerankChoice === ''
          ? {}
          : { rerank_model_id: wizard.rerankChoice === 'none' ? null : wizard.rerankChoice }),
      })
    }
    await client.post(`/companies/${wizard.companyId}/vector-library/knowledge-bases`, {
      name: wizard.name.trim(),
      description: wizard.description.trim() || null,
      chunk_parent_chars: wizard.parentChars,
      chunk_child_chars: wizard.childChars,
      chunk_child_overlap_chars: wizard.overlapChars,
    })
    wizard.visible = false
    selectedCompanyId.value = wizard.companyId
    ElMessage.success('知识库创建成功')
    await loadKbs()
  } catch (error) {
    errorMessage(error, '创建知识库失败')
  } finally {
    wizard.submitting = false
  }
}

onMounted(async () => {
  try {
    // 模型档案仅管理员可读；运营/只读不拉取，新建大库需管理员完成。
    const [companyRes, defaultsRes] = await Promise.all([
      client.get<Company[]>('/companies'),
      client.get<ChunkDefaults>('/admin/chunk-defaults'),
    ])
    companies.value = companyRes.data
    chunkDefaults.value = defaultsRes.data
    if (isFullAdmin.value) {
      const profileRes = await client.get<ModelProfile[]>('/admin/model-profiles')
      profiles.value = profileRes.data
    }
    if (!selectedCompanyId.value) {
      selectedCompanyId.value = activeCompanies.value[0]?.id ?? ''
    }
    await loadKbs()
  } catch (error) {
    errorMessage(error, '加载基础数据失败')
  }
})
</script>

<template>
  <div class="kb-page">
    <PageHeader
      title="知识库管理"
      eyebrow="公司子库"
      subtitle="按公司查看知识库 · 新建子库并管理其入库模型"
    >
      <template #meta>
        <span class="header-count">
          <template v-if="searchKeyword">「{{ searchKeyword }}」命中 {{ resultCount }} 个</template>
          <template v-else>共 {{ activeKbs.length }} 个</template>
        </span>
        <button v-if="searchKeyword" class="meta-clear" type="button" @click="clearKeyword">
          清除搜索
        </button>
      </template>
      <template #actions>
        <el-button v-if="canWrite" type="primary" size="small" @click="openWizard">
          <el-icon><Plus /></el-icon>新建知识库
        </el-button>
      </template>
    </PageHeader>

    <FilterBar>
      <el-select
        v-model="selectedCompanyId"
        placeholder="选择公司"
        class="company-select"
        @change="loadKbs"
      >
        <el-option
          v-for="company in activeCompanies"
          :key="company.id"
          :label="company.name"
          :value="company.id"
        />
      </el-select>
      <el-switch v-model="showArchived" active-text="显示已归档" @change="loadKbs" />
    </FilterBar>

    <DataPanel v-if="activeCompanies.length === 0">
      <EmptyState title="还没有可用公司" text="请先到「公司管理」创建公司。" />
    </DataPanel>
    <DataPanel
      v-else-if="!loading && !searchKeyword && activeKbs.length === 0 && archivedKbs.length === 0"
    >
      <EmptyState
        title="该公司还没有知识库"
        :text="canWrite ? '点击右上「新建知识库」创建第一个子库。' : '等待管理员创建子库。'"
      />
    </DataPanel>
    <template v-else>
      <DataPanel
        v-if="
          !loading &&
          filteredActiveKbs.length === 0 &&
          !(showArchived && filteredArchivedKbs.length > 0)
        "
      >
        <EmptyState
          :title="searchKeyword ? '没有匹配的知识库' : '暂无启用中的知识库'"
          :text="
            searchKeyword
              ? '换个关键词，或清除搜索查看全部。'
              : '打开上方「显示已归档」可查看归档库。'
          "
          :muted="Boolean(searchKeyword)"
        />
      </DataPanel>
      <el-row v-else :gutter="16" v-loading="loading">
        <el-col
          v-for="(kb, index) in filteredActiveKbs"
          :key="kb.id"
          :xs="24"
          :sm="12"
          :md="8"
          :lg="6"
        >
          <el-card
            class="kb-card stagger-item"
            shadow="hover"
            :style="{ '--delay': `${index * 40}ms` }"
          >
            <div class="kb-card-head">
              <span class="kb-icon"><el-icon><Collection /></el-icon></span>
              <span class="kb-name" :title="kb.name">{{ kb.name }}</span>
              <StatusPill
                :label="kb.status === 'active' ? '启用' : '已归档'"
                :tone="kb.status === 'active' ? 'success' : 'info'"
              />
            </div>
            <p class="kb-desc" :title="kb.description ?? ''">
              {{ kb.description || '暂无说明' }}
            </p>
            <div class="kb-meta">
              <div class="meta-row">
                <span class="meta-label">嵌入</span>
                <span class="meta-value" :title="profileName(kb.embedding_model_id)">{{ profileName(kb.embedding_model_id) }}</span>
              </div>
              <div class="meta-row">
                <span class="meta-label">Rerank</span>
                <span class="meta-value" :title="profileName(kb.rerank_model_id)">{{ profileName(kb.rerank_model_id) }}</span>
              </div>
              <div class="meta-row">
                <span class="meta-label">分块</span>
                <span class="meta-value" :title="chunkSummary(kb)">{{ chunkSummary(kb) }}</span>
              </div>
              <div class="meta-row kb-date">
                <span class="meta-label">创建</span>
                <span class="meta-value">{{ formatDate(kb.created_at) }}</span>
              </div>
            </div>
            <div class="kb-actions">
              <el-button size="small" type="primary" plain @click="router.push(`/kbs/${kb.id}`)">详情</el-button>
              <el-button
                v-if="canWrite"
                size="small"
                type="danger"
                plain
                @click="archiveKb(kb)"
              >
                归档
              </el-button>
            </div>
          </el-card>
        </el-col>
      </el-row>

      <template v-if="showArchived && filteredArchivedKbs.length > 0">
        <div class="archived-title">
          <span class="archived-dot"></span>已归档（{{ filteredArchivedKbs.length }}）
        </div>
        <el-row :gutter="16" v-loading="loading">
          <el-col
            v-for="(kb, index) in filteredArchivedKbs"
            :key="kb.id"
            :xs="24"
            :sm="12"
            :md="8"
            :lg="6"
          >
            <el-card
              class="kb-card archived-card stagger-item"
              shadow="never"
              :style="{ '--delay': `${index * 40}ms` }"
            >
              <div class="kb-card-head">
                <span class="kb-icon archived-icon"><el-icon><Collection /></el-icon></span>
                <span class="kb-name" :title="kb.name">{{ kb.name }}</span>
                <StatusPill label="已归档" tone="info" />
              </div>
              <p class="kb-desc" :title="kb.description ?? ''">
                {{ kb.description || '暂无说明' }}
              </p>
              <div class="kb-meta">
                <div class="meta-row">
                  <span class="meta-label">嵌入</span>
                  <span class="meta-value" :title="profileName(kb.embedding_model_id)">{{ profileName(kb.embedding_model_id) }}</span>
                </div>
                <div class="meta-row">
                  <span class="meta-label">Rerank</span>
                  <span class="meta-value" :title="profileName(kb.rerank_model_id)">{{ profileName(kb.rerank_model_id) }}</span>
                </div>
                <div class="meta-row">
                  <span class="meta-label">分块</span>
                  <span class="meta-value" :title="chunkSummary(kb)">{{ chunkSummary(kb) }}</span>
                </div>
                <div class="meta-row kb-date">
                  <span class="meta-label">归档</span>
                  <span class="meta-value">{{ kb.archived_at ? formatDate(kb.archived_at) : '—' }}</span>
                </div>
              </div>
              <div class="kb-actions">
                <el-button
                  v-if="canWrite"
                  size="small"
                  type="primary"
                  plain
                  @click="restoreKb(kb)"
                >
                  恢复
                </el-button>
                <el-button
                  v-if="canWrite"
                  size="small"
                  type="danger"
                  plain
                  @click="purgeKb(kb)"
                >
                  删除
                </el-button>
              </div>
            </el-card>
          </el-col>
        </el-row>
      </template>
    </template>

    <el-dialog
      v-model="wizard.visible"
      title="新建知识库"
      width="620px"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <el-steps :active="wizard.step" finish-status="success" align-center class="wizard-steps">
        <el-step v-for="title in wizardSteps" :key="title" :title="title" />
      </el-steps>

      <div v-if="wizard.step === 0" class="wizard-panel">
        <el-form label-width="90px">
          <el-form-item label="所属公司" required>
            <el-select
              v-model="wizard.companyId"
              placeholder="选择公司"
              style="width: 100%"
              @change="wizard.checked = false"
            >
              <el-option
                v-for="company in activeCompanies"
                :key="company.id"
                :label="company.name"
                :value="company.id"
              />
            </el-select>
          </el-form-item>
        </el-form>
        <el-alert
          v-if="wizard.checked && wizard.needLibrary"
          type="info"
          :closable="false"
          show-icon
          title="该公司还没有向量大库，下一步需要选择模型并创建大库。"
        />
        <el-alert
          v-else-if="wizard.checked && wizard.companyId"
          type="success"
          :closable="false"
          show-icon
          title="该公司已有向量大库，子库将直接继承其锁定模型。"
        />
      </div>

      <div v-else-if="wizard.step === 1 && wizard.needLibrary" class="wizard-panel">
        <template v-if="isFullAdmin">
          <el-form label-width="110px">
            <el-form-item label="大库名称" required>
              <el-input v-model="wizard.libraryName" placeholder="例如：公司名向量大库" />
            </el-form-item>
            <el-form-item label="Embedding" required>
              <el-select v-model="wizard.embeddingModelId" placeholder="选择 Embedding 模型" style="width: 100%">
                <el-option
                  v-for="profile in embeddingProfiles"
                  :key="profile.id"
                  :label="`${profile.name}（${profile.model_name}）`"
                  :value="profile.id"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="Rerank">
              <el-select v-model="wizard.rerankChoice" placeholder="使用默认 Rerank 模型" style="width: 100%">
                <el-option label="使用默认 Rerank 模型" value="" />
                <el-option
                  v-for="profile in rerankProfiles"
                  :key="profile.id"
                  :label="`${profile.name}（${profile.model_name}）`"
                  :value="profile.id"
                />
                <el-option label="不使用 Rerank" value="none" />
              </el-select>
            </el-form-item>
          </el-form>
          <el-alert
            v-if="embeddingProfiles.length === 0"
            type="error"
            :closable="false"
            show-icon
            title="没有启用的 Embedding 模型，请先到「模型管理」创建并启用。"
          />
          <el-alert
            type="warning"
            :closable="false"
            show-icon
            title="模型在大库创建时锁定；已有文档的库不能更换模型，更换需新建大库。"
          />
        </template>
        <el-alert
          v-else
          type="warning"
          :closable="false"
          show-icon
          title="该公司还没有向量大库；绑定模型属于管理员操作，请让管理员先创建大库后再新建子库。"
        />
      </div>

      <div v-else class="wizard-panel">
        <el-form label-width="110px">
          <el-form-item label="子库名称" required>
            <el-input v-model="wizard.name" placeholder="同一公司内不可重名" />
          </el-form-item>
          <el-form-item label="说明">
            <el-input v-model="wizard.description" type="textarea" :rows="2" placeholder="可选" />
          </el-form-item>
          <el-form-item label="父块字符数">
            <el-input-number v-model="wizard.parentChars" :min="1" :step="100" :value-on-clear="null" placeholder="留空跟随全局" />
          </el-form-item>
          <el-form-item label="子块字符数">
            <el-input-number v-model="wizard.childChars" :min="1" :step="100" :value-on-clear="null" placeholder="留空跟随全局" />
          </el-form-item>
          <el-form-item label="子块重叠数">
            <el-input-number v-model="wizard.overlapChars" :min="0" :step="10" :value-on-clear="null" placeholder="留空跟随全局" />
          </el-form-item>
        </el-form>
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="分块参数只影响之后新入库的文档；留空表示跟随服务端全局默认。"
        />
      </div>

      <template #footer>
        <el-button @click="wizard.visible = false">取消</el-button>
        <el-button v-if="wizard.step > 0" @click="onPrev">上一步</el-button>
        <el-button
          v-if="wizard.step < wizardSteps.length - 1"
          type="primary"
          :disabled="wizard.step === 0 && !wizard.companyId"
          @click="onNext"
        >
          下一步
        </el-button>
        <el-button
          v-else
          type="primary"
          :loading="wizard.submitting"
          :disabled="wizard.needLibrary && (!isFullAdmin || embeddingProfiles.length === 0)"
          @click="onCreate"
        >
          创建
        </el-button>
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
.meta-clear {
  padding: 0;
  border: none;
  background: transparent;
  font-size: var(--kb-font-2xs);
  color: var(--el-color-primary);
  cursor: pointer;
}
.meta-clear:hover {
  text-decoration: underline;
}
.company-select {
  width: 240px;
}
.archived-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 4px 0 14px;
  font-size: var(--kb-font-sm);
  font-weight: 600;
  color: var(--el-text-color-secondary);
}
.archived-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--el-text-color-placeholder);
}
.kb-card {
  height: 336px;
  box-sizing: border-box;
  margin-bottom: 16px;
  display: flex;
  flex-direction: column;
  transition: transform 0.15s var(--kb-ease), box-shadow 0.15s var(--kb-ease),
    border-color 0.15s var(--kb-ease);
}
/* 统一卡片高度：正文纵排，操作区沉底 */
.kb-card :deep(.el-card__body) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.kb-card:hover {
  transform: translateY(-1px);
  border-color: var(--el-border-color-dark);
  box-shadow: 0 6px 16px rgba(15, 23, 42, 0.08);
}
.stagger-item {
  animation: card-in 0.3s var(--kb-ease) backwards;
  animation-delay: var(--delay, 0ms);
}
@keyframes card-in {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
}
.kb-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.kb-icon {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  flex: none;
  display: grid;
  place-items: center;
  color: #4f46e5;
  font-size: 15px;
  background: #eef0fd;
}
.kb-name {
  flex: 1;
  font-weight: 600;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kb-desc {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  min-height: 20px;
  margin: 10px 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kb-meta {
  color: var(--el-text-color-regular);
  font-size: 12px;
  line-height: 2;
}
.meta-row {
  display: grid;
  grid-template-columns: 46px minmax(0, 1fr);
  column-gap: 4px;
}
.meta-label {
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}
.meta-value {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  word-break: break-word;
}
.kb-date {
  color: var(--el-text-color-secondary);
}
.kb-actions {
  margin-top: auto;
  padding-top: 10px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.archived-card {
  opacity: 0.9;
  border-style: dashed;
}
.archived-card:hover {
  border-color: var(--el-border-color-dark);
}
.archived-icon {
  background: var(--el-fill-color);
  color: var(--el-text-color-secondary);
}
@media (max-width: 767px) {
  .kb-card {
    height: auto;
  }
}
.wizard-steps {
  margin-bottom: 24px;
}
.wizard-panel {
  min-height: 180px;
}
</style>
