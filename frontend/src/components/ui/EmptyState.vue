<script setup lang="ts">
// 空态：几何竖条图形 + 说明 + 可选操作，替代插画式空状态。
withDefaults(
  defineProps<{
    title: string
    text?: string
    // muted 用于「无搜索结果」这类需要有别于「还没数据」的场景。
    muted?: boolean
  }>(),
  { muted: false },
)
</script>

<template>
  <div class="empty-state">
    <div class="empty-glyph" :class="{ 'is-muted': muted }" aria-hidden="true">
      <span /><span /><span />
    </div>
    <h3 class="empty-title">{{ title }}</h3>
    <p v-if="text" class="empty-text">{{ text }}</p>
    <div v-if="$slots.default" class="empty-action">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 48px 24px;
  text-align: center;
}

.empty-glyph {
  display: inline-flex;
  align-items: flex-end;
  gap: 5px;
  margin-bottom: 6px;
}

.empty-glyph span {
  display: block;
  width: 6px;
  border-radius: 2px;
  background: var(--el-color-primary-light-7);
}

.empty-glyph span:nth-child(1) {
  height: 12px;
}

.empty-glyph span:nth-child(2) {
  height: 22px;
  background: var(--el-color-primary-light-5);
}

.empty-glyph span:nth-child(3) {
  height: 17px;
}

.empty-glyph.is-muted span {
  background: #e3e5eb;
}

.empty-title {
  margin: 0;
  font-size: var(--kb-font-md);
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.empty-text {
  margin: 0;
  max-width: 460px;
  font-size: var(--kb-font-xs);
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.empty-action {
  margin-top: 6px;
}
</style>
