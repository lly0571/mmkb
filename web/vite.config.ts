import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

// The dev proxy target is build-time/deploy configuration, never client code: it is read from
// process.env or web/.env.local (prefix MMKB_) and is not exposed to the bundle.
// The backend router itself carries the /api prefix, so the path is forwarded unchanged.
export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), 'MMKB_'), ...process.env }
  const target = env.MMKB_API_PROXY_TARGET || 'http://127.0.0.1:8010'
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': { target, changeOrigin: true },
      },
    },
    build: { sourcemap: true },
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/tests/setup.ts'],
      css: false,
    },
  }
})
