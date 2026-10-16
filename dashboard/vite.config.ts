import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev proxy keeps the session cookie same-origin. In production the dashboard is served behind the same gateway as /v1.
const api = process.env.TELCO_API_URL ?? 'http://127.0.0.1:8000';
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/v1': api, '/charts': api, '/health': api } },
  preview: { proxy: { '/v1': api, '/charts': api, '/health': api } },
  build: { sourcemap: false, target: 'es2022' },
});
