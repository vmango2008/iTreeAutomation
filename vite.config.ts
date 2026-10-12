import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  // Project page (vmango2008.github.io/iTreeAutomation/), not a root user
  // page, so built asset URLs must be rooted under the repo name or they
  // 404 on GitHub Pages.
  base: '/iTreeAutomation/',
  plugins: [vue(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
