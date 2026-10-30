<script setup lang="ts">
import axios from 'axios'
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Lock, User } from '@element-plus/icons-vue'
import { client, errorMessage, setToken } from '../api/client'
import { loadIdentity } from '../api/session'

const emit = defineEmits<{ (e: 'authorized'): void }>()

// 默认走账号密码；Token 入口保留给脚本凭据与忘记账号时的兜底。
const mode = ref<'account' | 'token'>('account')
const username = ref('')
const password = ref('')
const token = ref('')
const loading = ref(false)

// 素材放在 public/images，构建后由 /admin-ui/ 前缀托管。
const heroUrl = `${import.meta.env.BASE_URL}images/login-hero.jpg`
const cubeUrl = `${import.meta.env.BASE_URL}images/login-cube.jpg`

async function finish(value: string) {
  setToken(value)
  // 身份统一由 /admin/me 提供，登录方式不同不影响后续角色判断。
  await loadIdentity()
  emit('authorized')
}

async function login() {
  loading.value = true
  try {
    if (mode.value === 'account') {
      const name = username.value.trim()
      if (!name || !password.value) {
        ElMessage.warning('请输入账号和密码')
        return
      }
      const { data } = await client.post<{ token: string }>('/admin/login', {
        username: name,
        password: password.value,
      })
      await finish(data.token)
      return
    }
    const value = token.value.trim()
    if (!value) {
      ElMessage.warning('请输入管理员 Token')
      return
    }
    // /admin/me 只接受管理员侧 Token（含运营/只读），公司 Token 会被 403 拒绝。
    await client.get('/admin/me', { headers: { Authorization: `Bearer ${value}` } })
    await finish(value)
  } catch (error) {
    // 401/429 的文案由后端给出，直接展示更准确。
    const status = axios.isAxiosError(error) ? error.response?.status : undefined
    if (status === 401 || status === 429) {
      ElMessage.error(status === 429 ? '失败次数过多，请稍后再试' : '账号或密码不正确')
    } else if (!axios.isAxiosError(error) || status !== 403) {
      errorMessage(error, mode.value === 'account' ? '登录失败，请检查账号密码' : '登录失败，请检查 Token')
    }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-scene">
    <!-- 整屏场景图：用 img 铺底，避免 CSS url() 拼变量在构建期解析失败。 -->
    <img class="scene-bg" :src="heroUrl" alt="" aria-hidden="true" />

    <header class="scene-brand">
      <span class="brand-mark">K</span>
      <span class="brand-name">知识库服务</span>
    </header>

    <!-- 3D 立方体与水面倒影：纯装饰，不吃交互。 -->
    <div class="scene-cube" aria-hidden="true">
      <img class="cube-img" :src="cubeUrl" alt="" />
      <img class="cube-reflection" :src="cubeUrl" alt="" />
    </div>

    <main class="scene-body">
      <section class="scene-copy">
        <h1 class="copy-title">
          <span class="t-navy">智汇知识</span>
          <span class="t-dot">·</span>
          <span class="t-violet">知识库</span>
          <span class="t-blue">服务管理端</span>
        </h1>
        <p class="copy-sub">文档自动入库，检索精准可溯，问答即问即答。</p>
      </section>

      <section class="scene-form">
        <div class="glass-card">
          <h2 class="card-title">欢迎登录</h2>
          <p class="card-sub">知识库服务管理端</p>

          <template v-if="mode === 'account'">
            <el-input
              v-model="username"
              size="large"
              :prefix-icon="User"
              placeholder="请输入账号"
              autocomplete="username"
              @keyup.enter="login"
            />
            <el-input
              v-model="password"
              type="password"
              size="large"
              :prefix-icon="Lock"
              show-password
              placeholder="请输入密码"
              autocomplete="current-password"
              class="field-gap"
              @keyup.enter="login"
            />
          </template>
          <el-input
            v-else
            v-model="token"
            type="password"
            show-password
            size="large"
            placeholder="请输入管理端 Token"
            autocomplete="off"
            @keyup.enter="login"
          />

          <el-button type="primary" size="large" class="login-btn" :loading="loading" @click="login">
            登录
          </el-button>

          <button
            type="button"
            class="mode-switch"
            @click="mode = mode === 'account' ? 'token' : 'account'"
          >
            {{ mode === 'account' ? '使用 Token 登录' : '使用账号密码登录' }}
          </button>
        </div>
      </section>
    </main>

    <footer class="scene-foot">YONKERCARE · KNOWLEDGE BASE SERVICE</footer>
  </div>
</template>

<style scoped>
.login-scene {
  position: fixed;
  inset: 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  overflow: hidden;
  background-color: #e8eeff;
}

.scene-bg {
  position: absolute;
  inset: 0;
  z-index: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center 42%;
  pointer-events: none;
  user-select: none;
}

/* ===== 左上品牌 ===== */
.scene-brand {
  position: absolute;
  left: clamp(24px, 4vw, 56px);
  top: clamp(22px, 3.4vw, 40px);
  display: flex;
  align-items: center;
  gap: 10px;
  z-index: 3;
  animation: fade-down 0.55s var(--kb-ease) backwards;
}
.brand-mark {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: linear-gradient(135deg, #5b58e8, #8f5cf0);
  color: #fff;
  font-size: 16px;
  font-weight: 700;
  box-shadow: 0 6px 16px rgba(91, 88, 232, 0.32);
}
.brand-name {
  font-size: var(--kb-font-lg);
  font-weight: 600;
  letter-spacing: 0.5px;
  color: #1c2452;
}

/* ===== 中央 3D ===== */
.scene-cube {
  position: absolute;
  left: 53%;
  top: 24%;
  z-index: 1;
  width: clamp(200px, 21vw, 380px);
  transform: translateX(-50%);
  pointer-events: none;
  /* 白底素材整组 multiply：白像素等价透明，免去抠图边缘。 */
  mix-blend-mode: multiply;
}
.cube-img {
  display: block;
  width: 100%;
  animation: float 7s ease-in-out infinite;
}
.cube-reflection {
  display: block;
  width: 100%;
  margin-top: -6%;
  opacity: 0.34;
  filter: blur(2px);
  transform: scaleY(-1);
  /* mask 在翻转前的局部坐标系里生效，所以方向要写反：靠立方体的一侧才最实。 */
  -webkit-mask-image: linear-gradient(0deg, rgba(0, 0, 0, 0.7), transparent 60%);
  mask-image: linear-gradient(0deg, rgba(0, 0, 0, 0.7), transparent 60%);
}

/* ===== 左文案 / 右卡片 ===== */
.scene-body {
  position: relative;
  z-index: 2;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 380px;
  align-items: center;
  gap: clamp(24px, 5vw, 72px);
  padding: 0 clamp(24px, 5vw, 72px);
}
.scene-copy {
  max-width: 560px;
  padding-right: 24px;
  animation: fade-up 0.6s var(--kb-ease) 0.06s backwards;
}
.copy-title {
  margin: 0;
  font-size: clamp(26px, 2.4vw, 38px);
  font-weight: 700;
  letter-spacing: 1px;
  line-height: 1.25;
  white-space: nowrap;
}
.t-navy {
  color: #1b2350;
}
.t-dot {
  margin: 0 10px;
  color: #9aa3c4;
}
.t-violet {
  background: linear-gradient(120deg, #6f5bf0, #9a5ce8);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.t-blue {
  background: linear-gradient(120deg, #3f7fe8, #4aa8e0);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.copy-sub {
  margin: 16px 0 0;
  font-size: var(--kb-font-md);
  letter-spacing: 0.4px;
  color: #6b7391;
}

.scene-form {
  display: flex;
  justify-content: flex-end;
  animation: fade-up 0.6s var(--kb-ease) 0.14s backwards;
}
.glass-card {
  box-sizing: border-box;
  width: 380px;
  padding: 34px 32px 32px;
  border: 1px solid rgba(255, 255, 255, 0.75);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.52);
  backdrop-filter: blur(16px) saturate(1.2);
  box-shadow: 0 20px 48px rgba(58, 70, 128, 0.16);
}
.card-title {
  margin: 0;
  text-align: center;
  font-size: var(--kb-font-xl);
  font-weight: 600;
  letter-spacing: 1px;
  color: #1b2350;
}
.card-sub {
  margin: 8px 0 26px;
  text-align: center;
  font-size: var(--kb-font-sm);
  color: #7c84a3;
}
.glass-card :deep(.el-input__wrapper) {
  background: rgba(255, 255, 255, 0.82);
  border: 1px solid rgba(255, 255, 255, 0.9);
  border-radius: 10px;
  box-shadow: none;
  transition: border-color 0.18s var(--kb-ease), box-shadow 0.18s var(--kb-ease),
    background-color 0.18s var(--kb-ease);
}
.glass-card :deep(.el-input__wrapper:hover) {
  background: #fff;
}
.glass-card :deep(.el-input.is-focus .el-input__wrapper) {
  background: #fff;
  border-color: var(--el-color-primary-light-5);
  box-shadow: 0 0 0 3px rgba(91, 88, 232, 0.12);
}
.field-gap {
  margin-top: 14px;
}
.login-btn {
  width: 100%;
  height: 44px;
  margin: 18px 0 0;
  border: none;
  border-radius: 10px;
  background: linear-gradient(96deg, #5b58e8 0%, #7d59ee 55%, #9a5ce8 100%);
  color: #fff;
  font-size: 15px;
  letter-spacing: 4px;
  box-shadow: 0 10px 24px rgba(99, 89, 232, 0.28);
  transition: transform 0.14s var(--kb-ease), box-shadow 0.2s var(--kb-ease),
    filter 0.2s var(--kb-ease);
}
.login-btn:hover {
  filter: brightness(1.06);
  box-shadow: 0 14px 30px rgba(99, 89, 232, 0.36);
}
.login-btn:active {
  transform: scale(0.985);
}

/* 登录方式切换：做成文字链，不引入额外控件宽度。 */
.mode-switch {
  display: block;
  width: 100%;
  margin: 14px 0 0;
  padding: 2px 0;
  border: none;
  background: none;
  font-size: var(--kb-font-sm);
  letter-spacing: 0.5px;
  color: #7c84a3;
  cursor: pointer;
  transition: color 0.18s var(--kb-ease);
}
.mode-switch:hover {
  color: var(--el-color-primary);
}

/* ===== 底部标识（参考图的底部胶囊位） ===== */
.scene-foot {
  position: absolute;
  left: 50%;
  bottom: 24px;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 20px;
  border: 1px solid rgba(255, 255, 255, 0.7);
  border-radius: 999px;
  transform: translateX(-50%);
  background: rgba(255, 255, 255, 0.42);
  backdrop-filter: blur(10px) saturate(1.15);
  box-shadow: 0 8px 22px rgba(58, 70, 128, 0.1);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: var(--kb-font-2xs);
  letter-spacing: 0.22em;
  color: rgba(43, 52, 94, 0.5);
}

@keyframes float {
  0%,
  100% {
    transform: translateY(-6px);
  }
  50% {
    transform: translateY(8px);
  }
}
@keyframes fade-up {
  from {
    opacity: 0;
    transform: translateY(14px);
  }
}
@keyframes fade-down {
  from {
    opacity: 0;
    transform: translateY(-10px);
  }
}

@media (max-width: 1180px) {
  .scene-body {
    grid-template-columns: minmax(0, 1fr) 360px;
  }
  .glass-card {
    width: 100%;
  }
  /* 窄屏把立方体缩到天空带里，避免压住标题和登录卡。 */
  .scene-cube {
    left: 50%;
    top: 13%;
    width: clamp(150px, 16vw, 200px);
  }
}

@media (max-width: 1000px) {
  .login-scene {
    justify-content: flex-start;
    padding-top: 96px;
  }
  .scene-body {
    grid-template-columns: minmax(0, 1fr);
    gap: 28px;
  }
  .scene-form {
    justify-content: center;
  }
  .scene-cube {
    display: none;
  }
  .copy-title {
    font-size: 26px;
    white-space: normal;
  }
}

@media (prefers-reduced-motion: reduce) {
  .cube-img,
  .scene-brand,
  .scene-copy,
  .scene-form {
    animation: none;
  }
  .login-btn:active {
    transform: none;
  }
}
</style>
