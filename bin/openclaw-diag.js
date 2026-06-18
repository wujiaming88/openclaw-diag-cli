#!/usr/bin/env node
// openclaw-diag — Node entry shell.
//
// All real logic lives in Python (ocdiag.main). This shell exists for one
// reason only: npx-friendly install. It locates a suitable python3, hands
// argv to the Python dispatcher, and forwards stdio + exit code transparently.
// The single source of truth for the module catalogue is `ocdiag/main.py`;
// the Node shell pulls the list from `ocdiag list --json` instead of
// duplicating it (axiom #3).

'use strict';

const { spawnSync, spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const REPO_ROOT = path.resolve(__dirname, '..');
const PKG = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, 'package.json'), 'utf8'));
const DISPATCHER = path.join(REPO_ROOT, 'bin', 'ocdiag');

const PYTHON_CANDIDATES = process.platform === 'win32'
  ? ['python3', 'python', 'py']
  : ['python3', 'python'];

function findPython() {
  for (const cmd of PYTHON_CANDIDATES) {
    try {
      const r = spawnSync(cmd, ['--version'], { stdio: ['ignore', 'pipe', 'pipe'] });
      if (r.status === 0) {
        const out = ((r.stdout || '') + (r.stderr || '')).trim();
        const m = out.match(/Python\s+(\d+)\.(\d+)\.(\d+)/);
        if (m) {
          const major = parseInt(m[1], 10);
          const minor = parseInt(m[2], 10);
          if (major > 3 || (major === 3 && minor >= 8)) {
            return { cmd, version: `${m[1]}.${m[2]}.${m[3]}` };
          }
        }
      }
    } catch (_) {
      // try next candidate
    }
  }
  return null;
}

function pythonNotFound() {
  console.error('Error: Python 3.8+ is required but was not found.');
  console.error('  Linux:   sudo apt install python3   /   sudo yum install python3');
  console.error('  macOS:   brew install python3       /   or install from https://www.python.org/downloads/');
  console.error('  Windows: https://www.python.org/downloads/  (be sure to check "Add to PATH")');
  console.error('  Re-run openclaw-diag once Python is installed.');
  process.exit(127);
}

function printVersion() {
  console.log(PKG.version);
}

function spawnDispatcher(pyCmd, args) {
  const child = spawn(pyCmd, [DISPATCHER, ...args], { stdio: 'inherit' });
  child.on('error', (err) => {
    console.error(`Error: failed to spawn ${pyCmd}: ${err.message}`);
    process.exit(1);
  });
  child.on('exit', (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code == null ? 1 : code);
  });
}

function runDoctor(pyCmd, args) {
  // Forward Node version into the Python doctor so it can include it in the
  // report. The v2 doctor reads OCDIAG_NODE_VERSION from the env.
  const env = Object.assign({}, process.env, { OCDIAG_NODE_VERSION: process.versions.node });
  const child = spawn(pyCmd, [DISPATCHER, 'doctor', ...args], { stdio: 'inherit', env });
  child.on('error', (err) => {
    console.error(`Error: failed to spawn ${pyCmd}: ${err.message}`);
    process.exit(1);
  });
  child.on('exit', (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code == null ? 1 : code);
  });
}

function main() {
  const argv = process.argv.slice(2);
  const py = findPython();

  if (argv.length === 0) {
    if (!py) pythonNotFound();
    console.log(`openclaw-diag v${PKG.version} — OpenClaw operations diagnostics CLI`);
    console.log('');
    spawnSync(py.cmd, [DISPATCHER, 'list'], { stdio: 'inherit' });
    console.log('');
    console.log('Common commands:');
    console.log('  openclaw-diag gateway           Run a single state collector');
    console.log('  openclaw-diag all               Run all state collectors');
    console.log('  openclaw-diag trace <uuid>      Trace one user message');
    console.log('  openclaw-diag doctor            Check the environment');
    console.log('  openclaw-diag --help            Full help');
    process.exit(0);
  }

  const head = argv[0];

  if (head === '--version' || head === '-v') {
    printVersion();
    process.exit(0);
  }
  if (head === '--help' || head === '-h') {
    if (!py) pythonNotFound();
    // Single source of truth for help is ocdiag/main.py (axiom #3): delegate
    // to the Python dispatcher's rich --help instead of duplicating it here.
    const r = spawnSync(py.cmd, [DISPATCHER, '--help'], { stdio: 'inherit' });
    process.exit(r.status == null ? 1 : r.status);
  }

  if (!py) pythonNotFound();

  if (head === 'doctor') {
    runDoctor(py.cmd, argv.slice(1));
    return;
  }

  if (head === 'skill-install') {
    const skillArgs = argv.slice(1);
    // Intercept --help/-h BEFORE spawning the installer. install-skill.py
    // has no flag parsing of its own, so spawning it with --help would
    // actually run the install — we print our own help here and exit.
    if (skillArgs.includes('--help') || skillArgs.includes('-h')) {
      const help = [
        'openclaw-diag skill-install — install the openclaw-diag skill into supported agent frameworks',
        '',
        'Usage:',
        '  openclaw-diag skill-install              Install into every detected framework',
        '  openclaw-diag skill-install --dry-run    Print target paths only; write nothing',
        '  openclaw-diag skill-install --help       This help',
        '',
        'Install targets (written only when the directory exists):',
        '  OpenClaw:    ~/.openclaw/skills/openclaw-diag/SKILL.md',
        '  Claude Code: ~/.claude/commands/openclaw-diag.md',
        '  Codex:       ~/.codex/instructions/openclaw-diag.md',
        '  Cursor:      ~/.cursor/rules/openclaw-diag.mdc',
      ];
      console.log(help.join('\n'));
      process.exit(0);
    }
    const skillScript = path.join(REPO_ROOT, 'scripts', 'install-skill.py');
    if (!fs.existsSync(skillScript)) {
      console.error(`Error: skill installer not found at ${skillScript}`);
      process.exit(1);
    }
    const r = spawnSync(py.cmd, [skillScript, ...skillArgs], { stdio: 'inherit' });
    process.exit(r.status == null ? 1 : r.status);
  }

  // Pass everything else (flat ids, `all`, `list`, `run` alias, unknown) to dispatcher.
  spawnDispatcher(py.cmd, argv);
}

main();
