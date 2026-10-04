import { createServer } from 'vite';
import { fileURLToPath } from 'node:url';
const port = Number(process.env.CONTEST_FRONTEND_PORT || 4178);
const backendPort = Number(process.env.CONTEST_BACKEND_PORT || 8018);
const server = await createServer({
  root: fileURLToPath(new URL('../', import.meta.url)),
  configFile: fileURLToPath(new URL('../vite.config.mjs', import.meta.url)),
  server: { host: '127.0.0.1', port, strictPort: true, proxy: {
    '/api/v1': { target: `http://127.0.0.1:${backendPort}`, changeOrigin: false },
    '/health': { target: `http://127.0.0.1:${backendPort}`, changeOrigin: false },
  } },
});
await server.listen();
server.printUrls();
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, async () => {
  await server.close(); process.exit(0);
});
