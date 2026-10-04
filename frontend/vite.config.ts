import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
    // icons.test.ts reads the generated glyph classes as text; Vitest stubs
    // every other stylesheet to an empty string.
    css: { include: [/icons\.css/] },
  },
  server: {
    proxy: {
      // Not the string shorthand: it implies changeOrigin, which rewrites Host
      // to 127.0.0.1:8000 while Origin stays this dev server's, and the API's
      // same-origin check (main._origin_is_trusted) then refuses every write.
      // Keeping the browser's Host makes a proxied request same-origin on
      // whatever port Vite is serving.
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false },
    },
  },
})
