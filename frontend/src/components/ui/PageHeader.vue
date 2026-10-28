<script setup lang="ts">
// 页面统一头部：左侧几何竖条 + 眉标/标题/说明，右侧动作区。
defineProps<{
  title: string
  eyebrow?: string
  subtitle?: string
}>()
</script>

<template>
  <header class="page-header">
    <div class="header-copy">
      <p v-if="eyebrow" class="header-eyebrow">{{ eyebrow }}</p>
      <div class="header-title-row">
        <h1 class="header-title">{{ title }}</h1>
        <slot name="meta" />
      </div>
      <p v-if="subtitle" class="header-sub">{{ subtitle }}</p>
    </div>
    <div v-if="$slots.actions" class="header-actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

.header-copy {
  min-width: 0;
}

.header-eyebrow {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 6px;
  font-size: var(--kb-font-2xs);
  letter-spacing: 0.12em;
  color: var(--el-text-color-secondary);
}

.header-eyebrow::before {
  content: '';
  width: 16px;
  height: 2px;
  border-radius: 2px;
  background: var(--el-color-primary);
}

.header-title-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}

.header-title {
  position: relative;
  margin: 0;
  padding-left: 12px;
  font-size: var(--kb-font-xl);
  font-weight: 600;
  line-height: 1.3;
  color: var(--el-text-color-primary);
}

/* 标题左侧 4px 靛蓝竖条：整套页面的几何锚点。 */
.header-title::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  width: 4px;
  height: 18px;
  border-radius: 2px;
  background: var(--el-color-primary);
  transform: translateY(-50%);
}

.header-sub {
  margin: 6px 0 0;
  font-size: var(--kb-font-xs);
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}

.header-actions :deep(.el-button) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
</style>
