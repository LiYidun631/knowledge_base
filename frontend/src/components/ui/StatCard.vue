<script setup lang="ts">
// 指标卡：固定高度保证同排底对齐，数字用等宽数字。
withDefaults(
  defineProps<{
    label: string
    value: string | number
    unit?: string
    hint?: string
    tone?: 'default' | 'primary' | 'success' | 'warning' | 'danger'
  }>(),
  { tone: 'default' },
)
</script>

<template>
  <div class="stat-card" :class="`tone-${tone}`">
    <span class="stat-mark" aria-hidden="true" />
    <span class="stat-label">{{ label }}</span>
    <p class="stat-value">
      {{ value }}<em v-if="unit" class="stat-unit">{{ unit }}</em>
    </p>
    <span v-if="hint" class="stat-hint">{{ hint }}</span>
  </div>
</template>

<style scoped>
.stat-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 2px;
  height: 96px;
  padding: 13px 16px;
  background: var(--kb-panel-bg);
  border: 1px solid var(--kb-panel-border);
  border-radius: var(--kb-panel-radius);
  box-shadow: var(--kb-panel-shadow);
  overflow: hidden;
}

.stat-mark {
  position: absolute;
  top: 14px;
  left: 16px;
  width: 6px;
  height: 6px;
  border-radius: 2px;
  background: var(--el-border-color-dark);
}

.tone-primary .stat-mark {
  background: var(--el-color-primary);
}

.tone-success .stat-mark {
  background: var(--el-color-success);
}

.tone-warning .stat-mark {
  background: var(--el-color-warning);
}

.tone-danger .stat-mark {
  background: var(--el-color-danger);
}

.stat-label {
  padding-left: 12px;
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
}

.stat-value {
  margin: auto 0 0;
  font-size: 22px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--el-text-color-primary);
  font-variant-numeric: tabular-nums;
}

.stat-unit {
  margin-left: 3px;
  font-size: var(--kb-font-xs);
  font-style: normal;
  font-weight: 400;
  color: var(--el-text-color-secondary);
}

.stat-hint {
  font-size: var(--kb-font-2xs);
  color: var(--el-text-color-placeholder);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
