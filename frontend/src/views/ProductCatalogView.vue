<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { client, errorMessage } from '../api/client'
import { canWrite } from '../api/session'
import PageHeader from '../components/ui/PageHeader.vue'
import DataPanel from '../components/ui/DataPanel.vue'
import FilterBar from '../components/ui/FilterBar.vue'
import EmptyState from '../components/ui/EmptyState.vue'

interface CatalogEntry {
  id: string
  product_name: string
  model_name: string
  created_at: string
}

const entries = ref<CatalogEntry[]>([])
const loading = ref(false)
const searchText = ref('')
const productFilter = ref('')
const selectedRows = ref<CatalogEntry[]>([])
const route = useRoute()

const addForm = reactive({
  productName: '',
  modelName: '',
  submitting: false,
})

const editForm = reactive({
  open: false,
  entry: null as CatalogEntry | null,
  productName: '',
  modelName: '',
  submitting: false,
})

const productNames = computed(() => {
  const names = new Set(entries.value.map((entry) => entry.product_name))
  return [...names].sort((a, b) => a.localeCompare(b, 'zh'))
})

const filteredEntries = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  return entries.value.filter((entry) => {
    if (productFilter.value && entry.product_name !== productFilter.value) return false
    if (!keyword) return true
    return (
      entry.product_name.toLowerCase().includes(keyword) ||
      entry.model_name.toLowerCase().includes(keyword)
    )
  })
})

// 筛选态区分「目录为空」和「筛不到」，空态文案不同。
const isFiltered = computed(() => Boolean(searchText.value.trim() || productFilter.value))

async function loadEntries() {
  loading.value = true
  try {
    const { data } = await client.get<CatalogEntry[]>('/product-catalog')
    entries.value = data
  } catch (error) {
    errorMessage(error, '加载产品目录失败')
  } finally {
    loading.value = false
  }
}

async function createEntry() {
  const productName = addForm.productName.trim()
  const modelName = addForm.modelName.trim()
  if (!productName || !modelName) {
    ElMessage.warning('请填写产品名称和产品型号')
    return
  }
  addForm.submitting = true
  try {
    await client.post('/product-catalog', {
      product_name: productName,
      model_name: modelName,
    })
    ElMessage.success('目录条目已添加')
    addForm.productName = ''
    addForm.modelName = ''
    await loadEntries()
  } catch (error) {
    errorMessage(error, '添加目录条目失败')
  } finally {
    addForm.submitting = false
  }
}

function openEdit(entry: CatalogEntry) {
  editForm.entry = entry
  editForm.productName = entry.product_name
  editForm.modelName = entry.model_name
  editForm.open = true
}

async function submitEdit() {
  const productName = editForm.productName.trim()
  const modelName = editForm.modelName.trim()
  if (!productName || !modelName) {
    ElMessage.warning('请填写产品名称和产品型号')
    return
  }
  if (!editForm.entry) return
  editForm.submitting = true
  try {
    await client.put(`/product-catalog/${editForm.entry.id}`, {
      product_name: productName,
      model_name: modelName,
    })
    ElMessage.success('目录条目已更新')
    editForm.open = false
    await loadEntries()
  } catch (error) {
    errorMessage(error, '更新目录条目失败')
  } finally {
    editForm.submitting = false
  }
}

async function removeEntry(entry: CatalogEntry) {
  try {
    await ElMessageBox.confirm(
      `删除「${entry.product_name} / ${entry.model_name}」？删除后上传文件不能再选择该标签，已上传文档不受影响。`,
      '删除目录条目',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.delete(`/product-catalog/${entry.id}`)
    ElMessage.success('目录条目已删除')
    await loadEntries()
  } catch (error) {
    errorMessage(error, '删除目录条目失败')
  }
}

async function batchRemove() {
  const rows = selectedRows.value
  if (rows.length === 0) return
  try {
    await ElMessageBox.confirm(
      `删除选中的 ${rows.length} 条目录条目？删除后上传文件不能再选择这些标签，已上传文档不受影响。`,
      '批量删除目录条目',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await client.post('/product-catalog/batch-delete', {
      catalog_ids: rows.map((entry) => entry.id),
    })
    ElMessage.success(`已删除 ${rows.length} 条目录条目`)
    await loadEntries()
  } catch (error) {
    errorMessage(error, '批量删除失败')
  }
}

function onSelectionChange(rows: CatalogEntry[]) {
  selectedRows.value = rows
}

function formatDateTime(value: string): string {
  return value.slice(0, 19).replace('T', ' ')
}

onMounted(() => {
  // 全局搜索跳转时带上关键词，进入即过滤目录。
  if (typeof route.query.q === 'string') searchText.value = route.query.q
  void loadEntries()
})
</script>

<template>
  <div class="catalog-page">
    <PageHeader
      title="产品目录"
      eyebrow="产品与型号"
      subtitle="全公司通用的一份目录，不挂在任何知识库下 · 上传文件时从这里选择产品名称与型号"
    >
      <template #meta>
        <span class="header-count">
          共 {{ entries.length }} 条<template v-if="isFiltered"> · 筛选后 {{ filteredEntries.length }} 条</template>
        </span>
      </template>
    </PageHeader>

    <DataPanel flush>
      <div class="panel-toolbar">
        <FilterBar>
          <template v-if="canWrite">
            <el-input
              v-model="addForm.productName"
              maxlength="200"
              placeholder="产品名称，如：体温计"
              class="add-input"
              @keyup.enter="createEntry"
            />
            <el-input
              v-model="addForm.modelName"
              maxlength="200"
              placeholder="产品型号，如：额式体温计"
              class="add-input"
              @keyup.enter="createEntry"
            />
            <el-button type="primary" :loading="addForm.submitting" @click="createEntry">
              添加条目
            </el-button>
            <span class="toolbar-divider" aria-hidden="true" />
          </template>
          <el-input
            v-model="searchText"
            clearable
            placeholder="搜索产品名称或型号"
            class="search-input"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-select
            v-model="productFilter"
            clearable
            placeholder="按产品名称筛选"
            class="filter-select"
          >
            <el-option v-for="name in productNames" :key="name" :label="name" :value="name" />
          </el-select>
          <template #actions>
            <el-button
              v-if="canWrite"
              type="danger"
              plain
              :disabled="selectedRows.length === 0"
              @click="batchRemove"
            >
              批量删除{{ selectedRows.length > 0 ? `（${selectedRows.length}）` : '' }}
            </el-button>
          </template>
        </FilterBar>
      </div>

      <el-table
        :data="filteredEntries"
        v-loading="loading"
        row-key="id"
        @selection-change="onSelectionChange"
      >
        <template #empty>
          <EmptyState
            :title="isFiltered ? '没有匹配的目录条目' : '目录还是空的'"
            :text="
              isFiltered
                ? '换个关键词，或清空产品名称筛选。'
                : canWrite
                  ? '在上方填写产品名称与型号，添加后上传文件即可选到该标签。'
                  : '等待管理员添加产品名称与型号。'
            "
            :muted="isFiltered"
          />
        </template>
        <el-table-column v-if="canWrite" type="selection" width="46" />
        <el-table-column prop="product_name" label="产品名称" min-width="200" show-overflow-tooltip />
        <el-table-column prop="model_name" label="产品型号" min-width="240" show-overflow-tooltip />
        <el-table-column label="登记时间" width="168">
          <template #default="{ row }">
            <span class="cell-time">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column v-if="canWrite" label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <div class="catalog-actions">
              <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" text type="danger" @click="removeEntry(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </DataPanel>

    <el-dialog v-model="editForm.open" title="修改目录条目" width="440px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="修改产品名称会同步更新目录中同名条目及已上传文档的产品标签；修改型号同步更新所有引用该型号的条目与文档标签。"
        class="edit-tip"
      />
      <div class="edit-field">
        <label class="edit-label">产品名称</label>
        <el-input v-model="editForm.productName" maxlength="200" placeholder="产品名称" />
      </div>
      <div class="edit-field">
        <label class="edit-label">产品型号</label>
        <el-input v-model="editForm.modelName" maxlength="200" placeholder="产品型号" />
      </div>
      <template #footer>
        <el-button @click="editForm.open = false">取消</el-button>
        <el-button type="primary" :loading="editForm.submitting" @click="submitEdit">
          保存
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
.panel-toolbar {
  padding: 14px 18px 0;
}
.add-input {
  width: 190px;
}
.search-input {
  width: 220px;
}
.filter-select {
  width: 170px;
}
.toolbar-divider {
  flex: none;
  width: 1px;
  height: 18px;
  background: var(--el-border-color);
}
.cell-time {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-regular);
  font-variant-numeric: tabular-nums;
}
/* 操作按钮保持单行 */
.catalog-actions {
  display: flex;
  flex-wrap: nowrap;
  align-items: center;
}
.catalog-actions :deep(.el-button) {
  margin-left: 0;
  padding-left: 5px;
  padding-right: 5px;
}
.catalog-actions :deep(.el-button + .el-button) {
  margin-left: 10px;
}
.edit-tip {
  margin-bottom: 14px;
}
.edit-field {
  margin-bottom: 12px;
}
.edit-label {
  display: block;
  margin-bottom: 6px;
  font-size: var(--kb-font-sm);
  color: var(--el-text-color-regular);
}
</style>
