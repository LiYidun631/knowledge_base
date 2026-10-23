<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { client, errorMessage } from '../api/client'

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

onMounted(loadEntries)
</script>

<template>
  <el-card>
    <template #header>
      <div class="head-row">
        <span class="head-title"><el-icon><Goods /></el-icon>产品目录</span>
        <span class="head-count">共 {{ entries.length }} 条</span>
      </div>
    </template>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="目录为全公司通用的一份，不挂在任何知识库下；任意知识库上传文件时从这里选择产品名称与型号。"
      class="tip"
    />

    <div class="controls-row">
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
      <el-divider direction="vertical" class="controls-divider" />
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
      <el-button
        type="danger"
        plain
        :disabled="selectedRows.length === 0"
        @click="batchRemove"
      >
        批量删除{{ selectedRows.length > 0 ? `（${selectedRows.length}）` : '' }}
      </el-button>
    </div>

    <el-table
      :data="filteredEntries"
      v-loading="loading"
      empty-text="目录为空，请在上方添加产品名称与型号"
      @selection-change="onSelectionChange"
    >
      <el-table-column type="selection" width="46" />
      <el-table-column prop="product_name" label="产品名称" min-width="200" />
      <el-table-column prop="model_name" label="产品型号" min-width="240" />
      <el-table-column label="登记时间" width="180">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <div class="catalog-actions">
            <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" text type="danger" @click="removeEntry(row)">删除</el-button>
          </div>
        </template>
      </el-table-column>
    </el-table>

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
.head-count {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.tip {
  margin-bottom: 14px;
}
.controls-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}
.controls-divider {
  height: 22px;
}
.add-input {
  width: 190px;
}
.search-input {
  width: 230px;
}
.filter-select {
  width: 170px;
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
  font-size: 13px;
  color: var(--el-text-color-regular);
}
</style>
