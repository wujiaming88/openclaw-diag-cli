#!/usr/bin/env node
// Postinstall hook — best-effort skill install.
//
// Invokes `bin/openclaw-diag.js skill-install` and silently swallows any
// failure. We never want a missing Python or unreadable home directory to
// fail an `npm install`. Users who want to see what happened can run
// `openclaw-diag skill-install` manually.

'use strict';

const { spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');

if (process.env.OPENCLAW_DIAG_SKIP_POSTINSTALL === '1') {
  process.exit(0);
}

const repoRoot = path.resolve(__dirname, '..');
const entry = path.join(repoRoot, 'bin', 'openclaw-diag.js');

if (!fs.existsSync(entry)) {
  process.exit(0);
}

try {
  spawnSync(process.execPath, [entry, 'skill-install'], {
    stdio: 'ignore',
    timeout: 15_000,
  });
} catch (_) {
  // best-effort, never block npm install
}
process.exit(0);
