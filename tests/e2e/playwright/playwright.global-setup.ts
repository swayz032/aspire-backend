import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';

import type { FullConfig } from '@playwright/test';

const LOCAL_BASE_URL = 'http://localhost:5000';
const SERVER_READY_TIMEOUT_MS = 180_000;
const POLL_INTERVAL_MS = 2_000;

function resolvePaths() {
  const rootDir = __dirname;
  const desktopDir = path.resolve(rootDir, '../../../../Aspire-desktop');
  const artifactsDir = path.resolve(rootDir, 'test-results');
  const pidFile = path.join(artifactsDir, 'webserver.pid');
  const logFile = path.join(artifactsDir, 'webserver.log');
  const tsxCli = path.join(desktopDir, 'node_modules', 'tsx', 'dist', 'cli.mjs');
  const serverEntry = path.join(desktopDir, 'server', 'index.ts');
  return { desktopDir, artifactsDir, pidFile, logFile, tsxCli, serverEntry };
}

async function waitForServer(url: string, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastError = 'server did not respond';

  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { redirect: 'manual' });
      if (response.ok || response.status === 302 || response.status === 307 || response.status === 308) {
        return;
      }
      lastError = `unexpected status ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }

  throw new Error(`Timed out waiting for ${url}: ${lastError}`);
}

export default async function globalSetup(_config: FullConfig): Promise<void> {
  const externalBaseUrl = process.env.BASE_URL?.trim();
  if (externalBaseUrl) {
    await waitForServer(externalBaseUrl, 30_000);
    return;
  }

  const { desktopDir, artifactsDir, pidFile, logFile, tsxCli, serverEntry } = resolvePaths();
  fs.mkdirSync(artifactsDir, { recursive: true });

  try {
    await waitForServer(LOCAL_BASE_URL, 5_000);
    return;
  } catch {
    // No local server running. Continue and start one.
  }

  const logStream = fs.createWriteStream(logFile, { flags: 'a' });
  const child = spawn(process.execPath, [tsxCli, serverEntry], {
    cwd: desktopDir,
    env: {
      ...process.env,
      PORT: '5000',
      PUBLIC_BASE_URL: LOCAL_BASE_URL,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });

  child.stdout?.pipe(logStream);
  child.stderr?.pipe(logStream);

  child.on('exit', (code, signal) => {
    logStream.write(`\n[playwright-global-setup] server exited code=${code} signal=${signal}\n`);
  });

  if (!child.pid) {
    throw new Error('Failed to start Aspire desktop web server: child process has no PID');
  }

  fs.writeFileSync(pidFile, String(child.pid), 'utf-8');
  await waitForServer(LOCAL_BASE_URL, SERVER_READY_TIMEOUT_MS);
}
