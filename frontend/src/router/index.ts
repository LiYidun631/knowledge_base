import { createRouter, createWebHashHistory } from 'vue-router'
import { adminIdentity } from '../api/session'

const CHUNK_TARGET_KEY = 'kb-admin:chunk-reload-to'

// 上次因路由 chunk 404 刷新时记录的目标页，在应用启动前恢复为初始路由，
// 避免先渲染旧页再跳转，打断页面过渡。
const pendingTarget = sessionStorage.getItem(CHUNK_TARGET_KEY)
if (pendingTarget?.startsWith('/')) {
  sessionStorage.removeItem(CHUNK_TARGET_KEY)
  history.replaceState(null, '', `#${pendingTarget}`)
}

// Hash 路由：刷新和深链接不需要服务端回退配置，由知识库服务静态托管时最省事。
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/dashboard' },
    {
      path: '/dashboard',
      name: 'dashboard',
      component: () => import('../views/DashboardView.vue'),
      meta: { group: '工作台', title: '统计概览' },
    },
    {
      path: '/search',
      name: 'search',
      component: () => import('../views/SearchView.vue'),
      meta: { group: '工作台', title: '全局搜索' },
    },
    {
      path: '/tasks',
      name: 'tasks',
      component: () => import('../views/TasksView.vue'),
      meta: { group: '工作台', title: '任务中心' },
    },
    {
      path: '/kbs',
      name: 'kbs',
      component: () => import('../views/KnowledgeBasesView.vue'),
      meta: { group: '工作台', title: '知识库管理' },
    },
    {
      path: '/kbs/:id',
      name: 'kb-detail',
      component: () => import('../views/KnowledgeBaseDetailView.vue'),
      meta: { group: '知识库管理', title: '知识库详情' },
    },
    {
      path: '/catalog',
      name: 'catalog',
      component: () => import('../views/ProductCatalogView.vue'),
      meta: { group: '工作台', title: '产品目录' },
    },
    {
      path: '/qa',
      name: 'qa',
      component: () => import('../views/QaTestView.vue'),
      meta: { group: '问答运营', title: '问答测试' },
    },
    {
      path: '/qa-records',
      name: 'qa-records',
      component: () => import('../views/QaRecordsView.vue'),
      meta: { group: '问答运营', title: '问答记录' },
    },
    {
      path: '/evaluation',
      name: 'evaluation',
      component: () => import('../views/EvaluationView.vue'),
      meta: { group: '问答运营', title: '检索评测' },
    },
    {
      path: '/project-traces',
      name: 'project-traces',
      component: () => import('../views/ProjectTracesView.vue'),
      meta: { group: '问答运营', title: '项目记录' },
    },
    {
      path: '/faqs',
      name: 'faqs',
      component: () => import('../views/FaqsView.vue'),
      meta: { group: '问答运营', title: 'FAQ 管理' },
    },
    {
      path: '/prompts',
      name: 'prompts',
      component: () => import('../views/PromptsView.vue'),
      meta: { group: '系统设置', title: '提示词管理', role: 'admin' },
    },
    {
      path: '/models',
      name: 'models',
      component: () => import('../views/ModelProfilesView.vue'),
      meta: { group: '系统设置', title: '模型管理', role: 'admin' },
    },
    {
      path: '/companies',
      name: 'companies',
      component: () => import('../views/CompaniesView.vue'),
      meta: { group: '系统设置', title: '公司管理' },
    },
    {
      path: '/admins',
      name: 'admins',
      component: () => import('../views/AdminCredentialsView.vue'),
      meta: { group: '系统设置', title: '管理员', role: 'admin' },
    },
    {
      path: '/audit',
      name: 'audit',
      component: () => import('../views/AuditLogsView.vue'),
      meta: { group: '系统设置', title: '审计日志', role: 'admin' },
    },
  ],
})

// 管理员专属页面：运营/只读即使手输地址也退回概览（后端同时会 403 兜底）。
router.beforeEach((to) => {
  const me = adminIdentity.value
  if (to.meta.role === 'admin' && me && me.role !== 'admin') {
    return { path: '/dashboard' }
  }
  return true
})

// 重新构建会删除旧的路由 chunk，旧标签页点菜单时动态导入 404，导航会静默失败。
// 检测到这类错误时整页刷新一次，10 秒内只重试一次，避免坏部署导致刷新循环。
const CHUNK_ERROR_RE =
  /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i
const CHUNK_RELOAD_KEY = 'kb-admin:chunk-reload-at'

router.onError((error, to) => {
  const message = error instanceof Error ? error.message : String(error)
  if (!CHUNK_ERROR_RE.test(message)) return
  const lastReload = Number(sessionStorage.getItem(CHUNK_RELOAD_KEY) || 0)
  if (Date.now() - lastReload < 10_000) return
  sessionStorage.setItem(CHUNK_RELOAD_KEY, String(Date.now()))
  if (to?.fullPath) sessionStorage.setItem(CHUNK_TARGET_KEY, to.fullPath)
  window.location.reload()
})

export default router
