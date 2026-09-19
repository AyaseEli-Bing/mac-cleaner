import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 构建产物直接输出到后端托管目录 mac-cleaner/static/
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../static',
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      // 开发态把 /api 代理到后端，保持同源同前缀
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
});
