<script setup lang="ts">
// 筛选条：左侧条件单行排列（窄屏横向滚动而不换行），右侧固定操作按钮。
</script>

<template>
  <div class="filter-bar">
    <div class="filter-fields">
      <slot />
    </div>
    <div v-if="$slots.actions" class="filter-actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}

.filter-fields {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1 1 auto;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
}

.filter-fields::-webkit-scrollbar {
  height: 0;
}

.filter-fields :deep(.el-select),
.filter-fields :deep(.el-input),
.filter-fields :deep(.el-date-editor) {
  flex: none;
}

.filter-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}
</style>
