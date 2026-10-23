<script setup lang="ts">
import axios from 'axios'
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { client, errorMessage, setToken } from '../api/client'

const emit = defineEmits<{ (e: 'authorized'): void }>()

const token = ref('')
const loading = ref(false)

async function login() {
  const value = token.value.trim()
  if (!value) {
    ElMessage.warning('请输入管理员 Token')
    return
  }
  loading.value = true
  try {
    // 用仅管理员可用的接口验证 Token：通过即管理员，失败走 401 拦截。
    await client.get('/companies', { headers: { Authorization: `Bearer ${value}` } })
    setToken(value)
    emit('authorized')
  } catch (error) {
    // 401 已由全局拦截器提示并清理，这里只兜底其余失败原因。
    if (!axios.isAxiosError(error) || error.response?.status !== 401) {
      errorMessage(error, '登录失败，请检查 Token')
    }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <el-card class="login-card">
      <div class="brand">
        <span class="brand-mark">K</span>
        <span class="brand-name">知识库服务管理端</span>
      </div>
      <p class="hint">请输入管理员 Token（仅保存在本浏览器 localStorage）</p>
      <el-input
        v-model="token"
        type="password"
        show-password
        size="large"
        placeholder="管理员 Token"
        @keyup.enter="login"
      />
      <el-button type="primary" size="large" class="login-btn" :loading="loading" @click="login">
        登录
      </el-button>
    </el-card>
  </div>
</template>

<style scoped>
.login-wrap {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f5f6f8;
}
.login-card {
  width: 380px;
  border-radius: 8px;
  border: 1px solid var(--el-border-color);
  box-shadow: 0 12px 32px rgba(15, 23, 42, 0.08);
}
.login-card :deep(.el-card__body) {
  padding: 32px 28px 28px;
}
.brand {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
}
.brand-mark {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  display: grid;
  place-items: center;
  font-size: 15px;
  font-weight: 700;
  color: #fff;
  background: #4f46e5;
}
.brand-name {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.2px;
  color: var(--el-text-color-primary);
}
.hint {
  margin: 10px 0 18px;
  text-align: center;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.login-btn {
  width: 100%;
  margin-top: 16px;
  height: 40px;
  font-size: 14px;
}
</style>
