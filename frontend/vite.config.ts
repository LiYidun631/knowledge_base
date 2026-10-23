import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 生产由服务端托管在 /admin-ui 子路径（根路径留给 API 语义）；开发时同样从 /admin-ui/ 访问。
export default defineConfig({
  plugins: [vue()],
  base: '/admin-ui/',
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8333',
        changeOrigin: true,
      },
    },
  },
})
