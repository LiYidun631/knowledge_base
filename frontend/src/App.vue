<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { clearToken, client, getToken, onAuthFailure } from './api/client'
import LoginView from './views/LoginView.vue'

const route = useRoute()
const router = useRouter()
const authorized = ref(false)

// 侧栏折叠：窄屏自动收起，宽屏恢复展开；用户也可手动切换。
const collapsed = ref(false)
let autoCollapsed = false

function syncViewport() {
  if (window.innerWidth < 1180) {
    if (!collapsed.value) {
      collapsed.value = true
      autoCollapsed = true
    }
  } else if (autoCollapsed) {
    collapsed.value = false
    autoCollapsed = false
  }
}

function toggleSidebar() {
  collapsed.value = !collapsed.value
  autoCollapsed = false
}

const menuGroups = [
  {
    title: '工作台',
    items: [
      { path: '/dashboard', label: '统计概览', icon: 'TrendCharts' },
      { path: '/kbs', label: '知识库管理', icon: 'Collection' },
      { path: '/catalog', label: '产品目录', icon: 'Goods' },
    ],
  },
  {
    title: '问答运营',
    items: [
      { path: '/qa', label: '问答测试', icon: 'ChatDotRound' },
      { path: '/qa-records', label: '问答记录', icon: 'Tickets' },
      { path: '/faqs', label: 'FAQ 管理', icon: 'ChatLineSquare' },
    ],
  },
  {
    title: '系统设置',
    items: [
      { path: '/prompts', label: '提示词管理', icon: 'EditPen' },
      { path: '/models', label: '模型管理', icon: 'Cpu' },
      { path: '/companies', label: '公司管理', icon: 'OfficeBuilding' },
      { path: '/admins', label: '管理员', icon: 'User' },
    ],
  },
]

// 知识库详情页仍高亮「知识库管理」。
const activeMenu = computed(() => (route.path.startsWith('/kbs/') ? '/kbs' : route.path))
const pageTitle = computed(() => String(route.meta.title ?? ''))
const pageGroup = computed(() => String(route.meta.group ?? ''))

const search = ref('')
const searchFocused = ref(false)

watch(
  () => route.query.q,
  (value) => {
    search.value = typeof value === 'string' ? value : ''
  },
  { immediate: true },
)

function submitSearch() {
  const q = search.value.trim()
  if (!q) return
  router.push({ path: '/kbs', query: { q } })
}

function clearSearch() {
  if (typeof route.query.q === 'string') {
    const query = { ...route.query }
    delete query.q
    router.replace({ path: route.path, query })
  }
}

function createKnowledgeBase() {
  router.push({ path: '/kbs', query: { create: '1' } })
}

// 401 时清除 Token 并退回登录页。
onAuthFailure(() => {
  authorized.value = false
})

onMounted(async () => {
  syncViewport()
  window.addEventListener('resize', syncViewport)
  // 已有 Token 时先验证一次，失效则被 401 拦截带回登录页。
  if (!getToken()) return
  try {
    await client.get('/companies')
    authorized.value = true
  } catch {
    /* 拦截器已处理提示与清理 */
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', syncViewport)
})

function logout() {
  clearToken()
  authorized.value = false
}
</script>

<template>
  <LoginView v-if="!authorized" @authorized="authorized = true" />
  <div v-else class="layout">
    <aside class="aside" :class="{ collapsed }">
      <div class="logo">
        <span class="logo-mark">K</span>
        <span v-show="!collapsed" class="logo-text">
          <span class="logo-name">知识库</span>
          <span class="logo-sub">Admin Console</span>
        </span>
      </div>

      <el-menu
        router
        :default-active="activeMenu"
        :collapse="collapsed"
        :collapse-transition="false"
        class="menu"
      >
        <el-menu-item-group v-for="group in menuGroups" :key="group.title">
          <template #title>
            <span v-show="!collapsed" class="menu-group-title">{{ group.title }}</span>
          </template>
          <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
            <el-icon><component :is="item.icon" /></el-icon>
            <template #title>{{ item.label }}</template>
          </el-menu-item>
        </el-menu-item-group>
      </el-menu>

      <div v-show="!collapsed" class="aside-foot">Yonkercare · Admin</div>
    </aside>

    <div class="shell">
      <header class="header">
        <div class="header-left">
          <button
            class="icon-btn"
            type="button"
            :title="collapsed ? '展开侧栏' : '收起侧栏'"
            @click="toggleSidebar"
          >
            <el-icon><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
          </button>
          <div class="crumb">
            <span class="crumb-group">{{ pageGroup }}</span>
            <span v-if="pageGroup" class="crumb-sep">/</span>
            <span class="crumb-title">{{ pageTitle }}</span>
          </div>
        </div>

        <div class="header-right">
          <el-input
            v-model="search"
            class="global-search"
            :class="{ focused: searchFocused }"
            placeholder="搜索知识库"
            clearable
            @focus="searchFocused = true"
            @blur="searchFocused = false"
            @keyup.enter="submitSearch"
            @clear="clearSearch"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>

          <el-button type="primary" :icon="'Plus'" class="create-btn" @click="createKnowledgeBase">
            <span class="create-label">新建知识库</span>
          </el-button>

          <el-dropdown trigger="click" @command="(cmd: string) => cmd === 'logout' && logout()">
            <button class="account" type="button">
              <span class="avatar">Y</span>
              <span class="account-name">管理员</span>
              <el-icon class="account-caret"><ArrowDown /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <main class="main">
        <router-view v-slot="{ Component }">
          <transition name="page" mode="out-in">
            <component :is="Component" :key="route.path" />
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>

<style scoped>
.layout {
  height: 100vh;
  display: flex;
  background: #f5f6f8;
}
.aside {
  width: 220px;
  flex: none;
  display: flex;
  flex-direction: column;
  background: #fff;
  border-right: 1px solid #e5e7eb;
  overflow: hidden;
  transition: width 0.25s var(--kb-ease);
}
.aside.collapsed {
  width: 64px;
}
.logo {
  height: 56px;
  flex: none;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 18px;
}
.logo-mark {
  width: 30px;
  height: 30px;
  flex: none;
  border-radius: 8px;
  display: grid;
  place-items: center;
  font-size: 15px;
  font-weight: 700;
  color: #fff;
  background: #4f46e5;
}
.logo-text {
  display: flex;
  flex-direction: column;
  line-height: 1.2;
  white-space: nowrap;
}
.logo-name {
  font-size: 15px;
  font-weight: 600;
  color: #1f2329;
  letter-spacing: 0.2px;
}
.logo-sub {
  margin-top: 2px;
  font-size: 10px;
  color: #a8adb7;
  letter-spacing: 0.4px;
}

.menu {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  border-right: none;
  padding: 4px 0 12px;
  --el-menu-bg-color: transparent;
  --el-menu-text-color: #4e5969;
  --el-menu-hover-bg-color: #f5f6f8;
  --el-menu-active-color: #4f46e5;
  --el-menu-item-height: 40px;
  --el-menu-base-level-padding: 10px;
}
.menu :deep(.el-menu-item-group__title) {
  padding: 14px 20px 6px;
  font-size: 11px;
  line-height: 1.4;
  color: #a8adb7;
  letter-spacing: 0.4px;
}
.menu :deep(.el-menu-item) {
  position: relative;
  height: 40px;
  margin: 2px 10px;
  border-radius: 6px;
  font-size: 13px;
  transition: background-color 0.15s var(--kb-ease), color 0.15s var(--kb-ease);
}
.menu :deep(.el-menu-item .el-icon) {
  font-size: 16px;
  margin-right: 10px;
}
.menu :deep(.el-menu-item:hover) {
  background: #f5f6f8;
}
.menu :deep(.el-menu-item.is-active) {
  background: #eef0fd;
  color: #4f46e5;
  font-weight: 500;
}
.menu :deep(.el-menu-item.is-active::before) {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  width: 3px;
  height: 16px;
  border-radius: 0 3px 3px 0;
  background: #4f46e5;
  transform: translateY(-50%);
}
.aside.collapsed .menu :deep(.el-menu-item) {
  justify-content: center;
  margin: 4px 8px;
  padding: 0 !important;
}
.aside.collapsed .menu :deep(.el-menu-item .el-icon) {
  margin-right: 0;
}
.aside.collapsed .menu :deep(.el-menu-item.is-active::before) {
  display: none;
}
.aside-foot {
  flex: none;
  padding: 12px 0 16px;
  text-align: center;
  font-size: 11px;
  color: #c4c8d0;
  letter-spacing: 0.4px;
  white-space: nowrap;
}

.shell {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.header {
  height: 56px;
  flex: none;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 24px 0 16px;
  background: #fff;
  border-bottom: 1px solid #e5e7eb;
}
.header-left,
.header-right {
  display: flex;
  align-items: center;
}
.header-left {
  gap: 12px;
  min-width: 0;
}
.header-right {
  margin-left: auto;
  gap: 12px;
}
.icon-btn {
  width: 32px;
  height: 32px;
  flex: none;
  display: grid;
  place-items: center;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #4e5969;
  font-size: 16px;
  cursor: pointer;
  transition: background-color 0.15s var(--kb-ease), color 0.15s var(--kb-ease);
}
.icon-btn:hover {
  background: #f5f6f8;
  color: #1f2329;
}
.crumb {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  font-size: 13px;
  white-space: nowrap;
}
.crumb-group {
  color: #8a8f99;
}
.crumb-sep {
  color: #c4c8d0;
}
.crumb-title {
  font-size: 15px;
  font-weight: 600;
  color: #1f2329;
  overflow: hidden;
  text-overflow: ellipsis;
}
.global-search {
  width: 260px;
}
.global-search :deep(.el-input__wrapper) {
  background: #f5f6f8;
  box-shadow: 0 0 0 1px transparent inset;
  border-radius: 6px;
  transition: background-color 0.2s var(--kb-ease), box-shadow 0.2s var(--kb-ease);
}
.global-search :deep(.el-input__wrapper:hover) {
  background: #eef0f3;
  box-shadow: 0 0 0 1px transparent inset;
}
.global-search.focused :deep(.el-input__wrapper) {
  background: #fff;
  box-shadow: 0 0 0 1px #4f46e5 inset;
}
.global-search :deep(.el-input__inner) {
  font-size: 13px;
}
.create-btn {
  height: 32px;
  padding: 0 14px;
}
.account {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 6px;
  border: none;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
  transition: background-color 0.15s var(--kb-ease);
}
.account:hover {
  background: #f5f6f8;
}
.avatar {
  width: 28px;
  height: 28px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: #eef0fd;
  color: #4f46e5;
  font-size: 12px;
  font-weight: 600;
}
.account-name {
  font-size: 13px;
  color: #4e5969;
}
.account-caret {
  font-size: 12px;
  color: #a8adb7;
}

.main {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 24px;
  background: #f5f6f8;
}

@media (max-width: 1080px) {
  .global-search {
    width: 200px;
  }
}
@media (max-width: 900px) {
  .crumb-group,
  .crumb-sep {
    display: none;
  }
  .account-name {
    display: none;
  }
}
@media (max-width: 760px) {
  .global-search {
    display: none;
  }
  .create-btn .create-label {
    display: none;
  }
  .create-btn {
    width: 32px;
    padding: 0;
  }
  .main {
    padding: 16px;
  }
}
</style>
