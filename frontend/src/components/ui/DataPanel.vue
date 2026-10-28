<script setup lang="ts">
// 数据面板：白底描边卡片，统一头部条、内容区与底部分页区。
defineProps<{
  title?: string
  subtitle?: string
  count?: string | number | null
  // 表格类内容自带留白，可关掉内容区内边距。
  flush?: boolean
}>()
</script>

<template>
  <section class="data-panel">
    <header v-if="title || $slots.actions" class="panel-head">
      <div class="panel-title">
        <h2 v-if="title">{{ title }}</h2>
        <span v-if="count !== null && count !== undefined" class="panel-count">{{ count }}</span>
        <span v-if="subtitle" class="panel-sub">{{ subtitle }}</span>
      </div>
      <div v-if="$slots.actions" class="panel-actions">
        <slot name="actions" />
      </div>
    </header>
    <div class="panel-body" :class="{ 'is-flush': flush }">
      <slot />
    </div>
    <footer v-if="$slots.footer" class="panel-foot">
      <slot name="footer" />
    </footer>
  </section>
</template>

<style scoped>
.data-panel {
  background: var(--kb-panel-bg);
  border: 1px solid var(--kb-panel-border);
  border-radius: var(--kb-panel-radius);
  box-shadow: var(--kb-panel-shadow);
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 18px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.panel-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
}

.panel-title h2 {
  position: relative;
  margin: 0;
  padding-left: 10px;
  font-size: var(--kb-font-md);
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.panel-title h2::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  width: 2px;
  height: 12px;
  border-radius: 2px;
  background: var(--el-color-primary-light-3);
  transform: translateY(-50%);
}

.panel-count {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
}

.panel-sub {
  font-size: var(--kb-font-xs);
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.panel-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}

.panel-body {
  padding: 16px 18px;
}

.panel-body.is-flush {
  padding: 0;
}

.panel-foot {
  display: flex;
  justify-content: flex-end;
  padding: 12px 18px;
  border-top: 1px solid var(--el-border-color-lighter);
}
</style>
