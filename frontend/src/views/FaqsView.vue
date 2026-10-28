<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { client, errorMessage } from '../api/client'
import FaqPanel from '../components/FaqPanel.vue'

interface Company {
  id: string
  code: string
  name: string
  status: string
}

interface KnowledgeBase {
  id: string
  company_id: string | null
  name: string
  status: string
}

const route = useRoute()
const companies = ref<Company[]>([])
const kbs = ref<KnowledgeBase[]>([])
const companyId = ref('')
const kbId = ref('')
const loading = ref(false)

const activeCompanies = computed(() => companies.value.filter((c) => c.status === 'active'))

// 全局搜索命中 FAQ 时带 q 跳转，进入后由 FaqPanel 预填搜索词。
const faqQuery = computed(() =>
  typeof route.query.q === 'string' && route.query.q ? route.query.q : undefined,
)

const activeKbs = computed(() =>
  kbs.value.filter((kb) => kb.company_id === companyId.value && kb.status === 'active'),
)

async function loadCompanies() {
  try {
    const { data } = await client.get<Company[]>('/companies')
    companies.value = data
    if (companyId.value && !activeCompanies.value.some((c) => c.id === companyId.value)) {
      companyId.value = ''
    }
    if (!companyId.value && activeCompanies.value.length > 0) {
      companyId.value = activeCompanies.value[0].id
    }
  } catch (error) {
    errorMessage(error, '加载公司列表失败')
  }
}

async function loadKbs() {
  if (!companyId.value) {
    kbs.value = []
    kbId.value = ''
    return
  }
  loading.value = true
  try {
    const { data } = await client.get<KnowledgeBase[]>('/knowledge-bases', {
      params: { company_id: companyId.value, limit: 200 },
    })
    kbs.value = data
    if (kbId.value && !activeKbs.value.some((kb) => kb.id === kbId.value)) kbId.value = ''
    if (!kbId.value && activeKbs.value.length > 0) kbId.value = activeKbs.value[0].id
  } catch (error) {
    errorMessage(error, '加载知识库列表失败')
  } finally {
    loading.value = false
  }
}

watch(companyId, loadKbs)

onMounted(async () => {
  // 从知识库详情跳转时带上 kb 参数，直接定位到对应知识库。
  const targetKb = route.query.kb
  if (typeof targetKb === 'string') kbId.value = targetKb
  await loadCompanies()
  if (kbId.value) {
    try {
      const { data } = await client.get<{ company_id: string | null }>(
        `/knowledge-bases/${kbId.value}`,
      )
      companyId.value = data.company_id ?? ''
    } catch {
      kbId.value = ''
    }
  }
  await loadKbs()
})
</script>

<template>
  <el-card>
    <template #header>
      <div class="head-row">
        <span class="head-title"><el-icon><ChatLineSquare /></el-icon>FAQ 管理</span>
        <span class="head-hint">
          库级 FAQ 对全库生效；文档级 FAQ 只在对应文档范围内可命中，命中后原样直出
        </span>
      </div>
    </template>

    <FaqPanel :key="kbId || 'empty'" :kb-id="kbId" :initial-query="faqQuery">
      <template #toolbar>
        <el-select
          v-model="companyId"
          size="small"
          placeholder="选择公司"
          class="picker-company"
          @change="kbId = ''"
        >
          <el-option
            v-for="company in activeCompanies"
            :key="company.id"
            :label="company.name"
            :value="company.id"
          />
        </el-select>
        <el-select
          v-model="kbId"
          size="small"
          placeholder="选择知识库"
          class="picker-kb"
          :loading="loading"
        >
          <el-option v-for="kb in activeKbs" :key="kb.id" :label="kb.name" :value="kb.id" />
        </el-select>
        <span class="picker-divider" aria-hidden="true" />
      </template>
    </FaqPanel>
  </el-card>
</template>

<style scoped>
.head-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
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
.picker-company,
.picker-kb {
  width: 180px;
}
.picker-divider {
  flex: none;
  width: 1px;
  height: 20px;
  background: var(--el-border-color);
}
</style>
