<script setup lang="ts">
// 状态胶囊：小圆点 + 文案，替代表格里的高饱和标签。
withDefaults(
  defineProps<{
    label: string
    tone?: 'success' | 'warning' | 'danger' | 'info' | 'primary'
    dot?: boolean
    // pulse 用于「运行中」这类进行中的状态，圆点做呼吸动画。
    pulse?: boolean
  }>(),
  { tone: 'info', dot: true, pulse: false },
)
</script>

<template>
  <span class="status-pill" :class="`tone-${tone}`">
    <i v-if="dot" class="pill-dot" :class="{ 'is-pulsing': pulse }" />
    {{ label }}
  </span>
</template>

<style scoped>
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: var(--kb-font-2xs);
  line-height: 18px;
  white-space: nowrap;
}

.pill-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}

.pill-dot.is-pulsing {
  animation: pill-pulse 1.1s ease-in-out infinite;
}

@keyframes pill-pulse {
  0%,
  100% {
    opacity: 0.35;
    transform: scale(0.8);
  }
  50% {
    opacity: 1;
    transform: scale(1.15);
  }
}

.tone-success {
  color: #15803d;
  background: var(--el-color-success-light-9);
}

.tone-warning {
  color: #b45309;
  background: var(--el-color-warning-light-9);
}

.tone-danger {
  color: #b91c1c;
  background: var(--el-color-danger-light-9);
}

.tone-info {
  color: #475569;
  background: #eef1f5;
}

.tone-primary {
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
</style>
