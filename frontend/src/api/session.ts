import { computed, readonly, ref } from 'vue'
import { client } from './client'

export type AdminRole = 'admin' | 'operator' | 'viewer'

export interface AdminIdentity {
  name: string
  role: AdminRole
  credential_id: string | null
  is_bootstrap: boolean
}

export const ROLE_LABELS: Record<AdminRole, string> = {
  admin: '管理员',
  operator: '运营',
  viewer: '只读',
}

// 身份来自 GET /admin/me；未加载或异常时按只读处理，避免误显示写入口。
const identity = ref<AdminIdentity | null>(null)

export const adminIdentity = readonly(identity)
export const adminRole = computed<AdminRole>(() => identity.value?.role ?? 'viewer')
export const canWrite = computed(() => adminRole.value !== 'viewer')
export const isFullAdmin = computed(() => adminRole.value === 'admin')

export function setIdentity(value: AdminIdentity | null): void {
  identity.value = value
}

export async function loadIdentity(): Promise<AdminIdentity> {
  const { data } = await client.get<AdminIdentity>('/admin/me')
  identity.value = data
  return data
}
