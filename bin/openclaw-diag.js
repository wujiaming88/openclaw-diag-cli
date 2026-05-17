#!/usr/bin/env node
// openclaw-diag — Node entry shell.
// Locates python3, forwards args to ocdiag.dispatcher, transparently passes stdio
// and exit code. Implements two Node-side commands (doctor, bundle dispatch,
// --version, --help) so the user gets useful UX even before Python runs.

'use strict';

const { spawnSync, spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const REPO_ROOT = path.resolve(__dirname, '..');
const PKG = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, 'package.json'), 'utf8'));

const PYTHON_CANDIDATES = process.platform === 'win32'
  ? ['python3', 'python', 'py']
  : ['python3', 'python'];

// Keep these in sync with ocdiag/dispatcher.py.
const STATE_COLLECTORS = [
  'sys_health', 'environment', 'configuration', 'gateway', 'recent_errors',
  'cron_jobs', 'performance', 'sessions', 'plugin_diag', 'shell_history',
];
const OBJECT_INSPECTORS = ['trace', 'extract'];
const MODULE_IDS = new Set([...STATE_COLLECTORS, ...OBJECT_INSPECTORS]);

const STATE_SCRIPTS = [
  '01_sys_health.py', '02_environment.py', '03_configuration.py',
  '04_gateway.py', '05_recent_errors.py', '06_cron_jobs.py',
  '07_performance.py', '08_sessions.py', '09_plugin_diag.py',
  '10_shell_history.py',
];
const OBJECT_SCRIPTS = ['oc_session_trace.py', 'oc_session_extract.py'];

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
      // try next
    }
  }
  return null;
}

function pythonNotFound() {
  console.error('Error: Python 3.8+ required but not found.');
  console.error('  Install: https://www.python.org/downloads/  or  apt install python3');
  process.exit(127);
}

function printVersion() {
  console.log(PKG.version);
}

function printHelp() {
  const lines = [
    'openclaw-diag — OpenClaw 诊断工具箱',
    '',
    'Usage:',
    '  openclaw-diag                          打印 banner + 诊断目录',
    '  openclaw-diag list                     列出全部诊断（按类型分组）',
    '  openclaw-diag <id> [args...]           跑单个诊断',
    '  openclaw-diag all [--skip a,b]         跑全部 state collectors',
    '  openclaw-diag all [--json]             NDJSON 聚合输出',
    '  openclaw-diag bundle <id>              打成 self-contained 单文件 .py',
    '  openclaw-diag doctor [--json]          检查 Node / Python / ocdiag / OpenClaw env',
    '  openclaw-diag --version                打印版本号',
    '  openclaw-diag --help                   本帮助',
    '',
    'State collectors (无需参数):',
    '  ' + STATE_COLLECTORS.join('  '),
    '',
    'Object inspectors (需要 session uuid):',
    '  ' + OBJECT_INSPECTORS.join('  '),
    '',
    '透传给诊断脚本: --config --log-dir --json --no-color',
  ];
  console.log(lines.join('\n'));
}

function runDispatcher(args) {
  const py = findPython();
  if (!py) pythonNotFound();
  const dispatcher = path.join(REPO_ROOT, 'bin', 'ocdiag');
  const child = spawn(py.cmd, [dispatcher, ...args], { stdio: 'inherit' });
  child.on('error', (err) => {
    console.error(`Error: failed to spawn ${py.cmd}: ${err.message}`);
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

function runScript(scriptPath, args) {
  const py = findPython();
  if (!py) pythonNotFound();
  const child = spawn(py.cmd, [scriptPath, ...args], { stdio: 'inherit' });
  child.on('error', (err) => {
    console.error(`Error: failed to spawn ${py.cmd}: ${err.message}`);
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

function runBundle(args) {
  if (args.length === 0) {
    console.error('Error: bundle requires a module id (e.g. `openclaw-diag bundle gateway`)');
    process.exit(2);
  }
  runScript(path.join(REPO_ROOT, 'lib', 'bundle.py'), args);
}

// ── doctor ──

function nodeVersionOk() {
  const m = process.versions.node.match(/^(\d+)\./);
  return m && parseInt(m[1], 10) >= 18;
}

function checkOcdiagImport(pyCmd) {
  const r = spawnSync(
    pyCmd,
    ['-c', 'import sys, os; sys.path.insert(0, os.environ["OCDIAG_REPO_ROOT"]); import ocdiag; print(ocdiag.__version__)'],
    {
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, OCDIAG_REPO_ROOT: REPO_ROOT },
    },
  );
  if (r.status === 0) {
    return { ok: true, version: (r.stdout || '').toString().trim() };
  }
  return { ok: false, error: ((r.stderr || '') + (r.stdout || '')).toString().trim() };
}

function checkDiagScripts(pyCmd) {
  const failed = [];
  const all = [
    ...STATE_SCRIPTS.map((n) => ({ name: n, path: path.join(REPO_ROOT, 'diag', n) })),
    ...OBJECT_SCRIPTS.map((n) => ({ name: n, path: path.join(REPO_ROOT, 'tools', n) })),
  ];
  for (const item of all) {
    const r = spawnSync(pyCmd, [item.path, '--help'], {
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 10000,
    });
    if (r.status !== 0) {
      failed.push({ script: item.name, status: r.status, stderr: ((r.stderr || '').toString().trim()).slice(0, 200) });
    }
  }
  return { failed, total: all.length };
}

function checkOpenclawConfig() {
  const home = process.env.HOME || require('os').homedir();
  const cfg = process.env.OPENCLAW_CONFIG
    || path.join(process.env.OPENCLAW_HOME || path.join(home, '.openclaw'), 'openclaw.json');
  return { path: cfg, exists: fs.existsSync(cfg) };
}

function runDoctor(args) {
  const jsonMode = args.includes('--json');
  const result = {
    node: { version: process.versions.node, ok: nodeVersionOk() },
    python: null,
    ocdiag: null,
    diag_scripts: null,
    openclaw_config: null,
  };

  const py = findPython();
  if (!py) {
    result.python = { ok: false, error: 'Python 3.8+ not found in PATH' };
    if (jsonMode) {
      console.log(JSON.stringify(result, null, 2));
    } else {
      console.log(`✓ Node v${result.node.version}${result.node.ok ? '' : ' (need >= 18)'}`);
      console.log('✗ Python 3.8+ not found in PATH');
      console.log('  Install: https://www.python.org/downloads/  or  apt install python3');
    }
    process.exit(1);
  }
  result.python = { ok: true, version: py.version, cmd: py.cmd };

  const ocdiag = checkOcdiagImport(py.cmd);
  result.ocdiag = ocdiag;

  const { failed, total } = checkDiagScripts(py.cmd);
  result.diag_scripts = {
    ok: failed.length === 0,
    total,
    failed,
  };

  const cfg = checkOpenclawConfig();
  result.openclaw_config = cfg;

  if (jsonMode) {
    console.log(JSON.stringify(result, null, 2));
  } else {
    console.log(`${result.node.ok ? '✓' : '✗'} Node v${result.node.version}${result.node.ok ? '' : ' (need >= 18)'}`);
    console.log(`✓ Python ${py.version} (${py.cmd})`);
    if (ocdiag.ok) {
      console.log(`✓ ocdiag package importable (version ${ocdiag.version})`);
    } else {
      console.log('✗ ocdiag package not importable');
      if (ocdiag.error) {
        console.log('  ' + ocdiag.error.split('\n').slice(-3).join(' | '));
      }
    }
    if (failed.length === 0) {
      console.log(`✓ All ${total} diagnostics respond to --help`);
    } else {
      console.log(`✗ ${failed.length}/${total} diagnostics failed --help:`);
      for (const f of failed) {
        console.log(`    ${f.script} (rc=${f.status})`);
      }
    }
    if (cfg.exists) {
      console.log(`✓ OpenClaw config present (${cfg.path})`);
    } else {
      console.log(`ℹ OpenClaw config not found (${cfg.path}) — diagnostics will run but report missing`);
    }
  }

  const ok = result.node.ok && result.python.ok && ocdiag.ok && failed.length === 0;
  process.exit(ok ? 0 : 1);
}

// ── main ──

function main() {
  const argv = process.argv.slice(2);

  if (argv.length === 0) {
    const py = findPython();
    if (!py) pythonNotFound();
    console.log(`openclaw-diag v${PKG.version} — OpenClaw 诊断工具箱`);
    console.log('');
    const dispatcher = path.join(REPO_ROOT, 'bin', 'ocdiag');
    spawnSync(py.cmd, [dispatcher, 'list'], { stdio: 'inherit' });
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
    printHelp();
    process.exit(0);
  }
  if (head === 'doctor') {
    runDoctor(argv.slice(1));
    return;
  }
  if (head === 'bundle') {
    runBundle(argv.slice(1));
    return;
  }

  // Pass through everything else (flat ids, `all`, `list`, `run` alias, unknown) to dispatcher.
  runDispatcher(argv);
}

main();
