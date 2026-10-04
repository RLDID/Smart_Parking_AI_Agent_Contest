import { defineConfig } from 'vite';
export default defineConfig({
  base: './',
  server: { proxy: {
    '/api/v1': { target: 'http://127.0.0.1:8010', changeOrigin: false },
    '/devtools': { target: 'http://127.0.0.1:8010', changeOrigin: false },
    '/health': { target: 'http://127.0.0.1:8010', changeOrigin: false },
  } },
  build: { outDir: 'dist', emptyOutDir: true },
});
