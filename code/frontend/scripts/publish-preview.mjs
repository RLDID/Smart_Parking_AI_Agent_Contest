import { cp, mkdir } from 'node:fs/promises';
// Preserve redistribution notices beside the frontend build.
const output = new URL('../dist/licenses/', import.meta.url);
await mkdir(output, { recursive: true });
for (const name of ['Poppins-OFL.txt', 'SUIT-OFL.txt']) {
  await cp(new URL(`../assets/fonts/${name}`, import.meta.url), new URL(name, output));
}
