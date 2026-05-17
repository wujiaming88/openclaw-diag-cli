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
    'openclaw-diag — OpenClaw / ArkClaw read-only diagnostic CLI',
    '',
    'Usage:',
    '  openclaw-diag                          Show banner + module list',
    '  openclaw-diag list                     List all diagnostic modules',
    '  openclaw-diag run <id>                 Run a single module (or "all")',
    '  openclaw-diag run all [--skip a,b]     Run all modules (skip optional)',
    '  openclaw-diag run <id> --json          Emit JSON (NDJSON for "all")',
    '  openclaw-diag bundle <id>              Print self-contained single-file .py to stdout',
    '  openclaw-diag doctor [--json]          Check Node / Python / ocdiag / OpenClaw env',
    '  openclaw-diag --version                Print package version',
    '  openclaw-diag --help                   Print this help',
    '',
    'Module ids: sys_health environment configuration gateway recent_errors',
    '            cron_jobs performance sessions plugin_diag shell_history',
    '',
    'Pass-through flags (forwarded to Python): --config --log-dir --json --no-color',
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

function runBundle(args) {
  if (args.length === 0) {
    console.error('Error: bundle requires a module id (e.g. `openclaw-diag bundle gateway`)');
    process.exit(2);
  }
  const py = findPython();
  if (!py) pythonNotFound();
  const bundleScript = path.join(REPO_ROOT, 'lib', 'bundle.py');
  const child = spawn(py.cmd, [bundleScript, ...args], { stdio: 'inherit' });
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

// ── doctor ──

const DIAG_SCRIPTS = [
  '01_sys_health.py', '02_environment.py', '03_configuration.py',
  '04_gateway.py', '05_recent_errors.py', '06_cron_jobs.py',
  '07_performance.py', '08_sessions.py', '09_plugin_diag.py',
  '10_shell_history.py',
];

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
  for (const name of DIAG_SCRIPTS) {
    const p = path.join(REPO_ROOT, 'diag', name);
    const r = spawnSync(pyCmd, [p, '--help'], {
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 10000,
    });
    if (r.status !== 0) {
      failed.push({ script: name, status: r.status, stderr: ((r.stderr || '').toString().trim()).slice(0, 200) });
    }
  }
  return failed;
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

  const failed = checkDiagScripts(py.cmd);
  result.diag_scripts = {
    ok: failed.length === 0,
    total: DIAG_SCRIPTS.length,
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
      console.log(`✓ All ${DIAG_SCRIPTS.length} diag modules respond to --help`);
    } else {
      console.log(`✗ ${failed.length}/${DIAG_SCRIPTS.length} diag modules failed --help:`);
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
    console.log(`openclaw-diag v${PKG.version} — OpenClaw / ArkClaw 诊断 CLI`);
    console.log('');
    const dispatcher = path.join(REPO_ROOT, 'bin', 'ocdiag');
    spawnSync(py.cmd, [dispatcher, 'list'], { stdio: 'inherit' });
    console.log('');
    console.log('常用命令：');
    console.log('  openclaw-diag run gateway       跑单个模块');
    console.log('  openclaw-diag run all           全部模块');
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

  // Pass through everything else to the Python dispatcher.
  runDispatcher(argv);
}

main();
