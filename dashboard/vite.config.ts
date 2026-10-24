import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig(({ mode }) => {
  // Opt-in local dev/preview proxy only. Nothing is injected into the production bundle.
  const env = loadEnv(mode, '.', 'LIVEPROBE_');
  const mock = env.LIVEPROBE_MOCK_API;
  return { plugins: [react()], server: { proxy: { '/api': mock || 'http://localhost:8000' } }, preview: { proxy: mock ? { '/api': mock } : undefined } };
});
