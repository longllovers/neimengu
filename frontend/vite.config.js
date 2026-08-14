import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [],
  build: {
    outDir: null,
    emptyOutDir: null,
    sourcemap: null,
  },
  server: {
    proxy: {
      '/api': null,
    },
  },
})
