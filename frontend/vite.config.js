import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// During `npm run dev`, proxy API routes to the FastAPI backend on :8000.
// In production the built app is served by FastAPI itself (same origin).
const api = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/chat': api,
      '/health': api,
      '/brand': api,
      '/reset': api,
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
