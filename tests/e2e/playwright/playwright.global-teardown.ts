import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

import type { FullConfig } from '@playwright/test';

function resolvePidFile(): string {
  return path.resolve(__dirname, 'test-results', 'webserver.pid');
}

export default async function globalTeardown(_config: FullConfig): Promise<void> {
  if (process.env.BASE_URL?.trim()) {
    return;
  }

  const pidFile = resolvePidFile();
  if (!fs.existsSync(pidFile)) {
    return;
  }

  const rawPid = fs.readFileSync(pidFile, 'utf-8').trim();
  fs.rmSync(pidFile, { force: true });

  const pid = Number(rawPid);
  if (!Number.isInteger(pid) || pid <= 0) {
    return;
  }

  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' });
    return;
  }

  try {
    process.kill(pid, 'SIGTERM');
  } catch {
    // Process already gone.
  }
}
