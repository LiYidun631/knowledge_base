import axios from 'axios'
import { ElMessage } from 'element-plus'

// 管理员 Token 只保存在浏览器 localStorage，不落任何后端或日志。
export const TOKEN_KEY = 'kb_admin_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export const client = axios.create({ baseURL: '/api/v1' })

client.interceptors.request.use((config) => {
  const token = getToken()
  // 登录校验会显式传 Token，不能被 localStorage 里的旧凭据覆盖。
  if (token && !config.headers.Authorization) config.headers.Authorization = `Bearer ${token}`
  return config
})

// 401 说明凭据失效（会话过期或 Token 被撤销），清除并回到登录页；403 说明角色不够，仅提示。
let unauthorizedHandler: (() => void) | null = null

export function handleAuthStatus(status: number): void {
  if (status === 401) {
    clearToken()
    ElMessage.error('登录已过期或凭据失效，请重新登录')
    unauthorizedHandler?.()
  } else if (status === 403) {
    ElMessage.error('权限不足：该操作需要更高的角色')
  }
}

export function onAuthFailure(handleUnauthorized: () => void): void {
  unauthorizedHandler = handleUnauthorized
  client.interceptors.response.use(
    (response) => response,
    (error) => {
      const status: number | undefined = error.response?.status
      if (status === 401 || status === 403) handleAuthStatus(status)
      return Promise.reject(error)
    },
  )
}

export function errorMessage(error: unknown, fallback: string): void {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'string' && detail) {
      ElMessage.error(detail)
      return
    }
  }
  ElMessage.error(fallback)
}

// 只取错误文案不弹提示，供批量上传逐条展示失败原因。
export function errorText(error: unknown, fallback: string): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'string' && detail) return detail
    if (error.message) return error.message
  }
  return fallback
}
