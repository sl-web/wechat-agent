import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期代理: /api 转发到后端 FastAPI(默认 8000)。
// API_TARGET 可覆盖 —— 需要并行跑第二个后端(验证隔离环境)时很有用,不必改文件。
const apiTarget = process.env.API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
  },
})
