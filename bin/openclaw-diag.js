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
  console.error('Error: 需要 Python 3.8+ 但未找到。');
  console.error('  Linux:   sudo apt install python3   /   sudo yum install python3');
  console.error('  macOS:   brew install python3       /   或从 https://www.python.org/downloads/ 安装');
  console.error('  Windows: https://www.python.org/downloads/  （记得勾上 "Add to PATH"）');
  console.error('  装完后再次运行 openclaw-diag 即可。');
  process.exit(127);
}

function fetchModules(pyCmd) {
  const r = spawnSync(pyCmd, [DISPATCHER, 'list', '--json'], {
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (r.status !== 0) return null;
  try {
    return JSON.parse((r.stdout || '').toString());
  } catch (_) {
    return null;
  }
}

function printVersion() {
  console.log(PKG.version);
}

function printHelp(modules) {
  const state = modules ? modules.state_collectors.map((m) => m.id) : [];
  const obj = modules ? modules.object_inspectors.map((m) => m.id) : [];
  const lines = [
    'openclaw-diag — OpenClaw 诊断工具箱',
    '',
    '用法：',
    '  openclaw-diag                          打印 banner + 诊断目录',
    '  openclaw-diag <id> [args...]           跑单个诊断',
    '  openclaw-diag all [--skip a,b]         跑全部 state collectors',
    '  openclaw-diag list                     列出所有诊断',
    '  openclaw-diag doctor                   检查 Node / Python / 环境',
    '  openclaw-diag --version                打印版本号',
    '  openclaw-diag --help                   本帮助',
    '',
    '扫描类（无需参数）：',
    '  ' + (state.length ? state.join('  ') : '（无法连接到 Python）'),
    '',
    '对象类（需要 session uuid）：',
    '  ' + (obj.length ? obj.join('  ') : '（无法连接到 Python）'),
    '',
    '常用 flag：--json（结构化输出）  --no-color（关掉颜色）  --unmask（不脱敏）',
  ];
  console.log(lines.join('\n'));
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
    console.log(`openclaw-diag v${PKG.version} — OpenClaw 诊断工具箱`);
    console.log('');
    spawnSync(py.cmd, [DISPATCHER, 'list'], { stdio: 'inherit' });
    console.log('');
    console.log('常用命令：');
    console.log('  openclaw-diag gateway           跑单个 state collector');
    console.log('  openclaw-diag all               全部 state collectors');
    console.log('  openclaw-diag trace <uuid>      追踪一条用户消息');
    console.log('  openclaw-diag doctor            检查环境');
    console.log('  openclaw-diag --help            完整帮助');
    process.exit(0);
  }

  const head = argv[0];

  if (head === '--version' || head === '-v') {
    printVersion();
    process.exit(0);
  }
  if (head === '--help' || head === '-h') {
    if (!py) pythonNotFound();
    printHelp(fetchModules(py.cmd));
    process.exit(0);
  }

  if (!py) pythonNotFound();

  if (head === 'doctor') {
    runDoctor(py.cmd, argv.slice(1));
    return;
  }

  // Pass everything else (flat ids, `all`, `list`, `run` alias, unknown) to dispatcher.
  spawnDispatcher(py.cmd, argv);
}

main();
