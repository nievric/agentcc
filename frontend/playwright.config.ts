import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:5179", trace: "retain-on-failure" },
  webServer: [
    { command: "PYTHONPATH=../backend ../.venv/bin/python ../backend/tests/e2e_server.py", url: "http://127.0.0.1:19090/health" },
    { command: "npm run dev -- --config e2e/vite.config.ts --host 127.0.0.1 --port 5179 --strictPort", url: "http://127.0.0.1:5179" }
  ]
});
