import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Test-only config. `npm run build` keeps using vite.config.ts.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
