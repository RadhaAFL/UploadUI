import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    target: 'esnext',
  },
  base: '/uploadUI/',
  server: {
    port: 3000,
    proxy: {
      '/uploadportal-api': {
        target: 'http://localhost:5003',
        rewrite: (path) => path.replace(/^\/uploadportal-api/, ''),
      },
    },
  },
})
