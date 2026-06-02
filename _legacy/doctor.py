"""``ocdiag doctor`` — environment health-check.

Sole authoritative implementation. Both Node (`bin/openclaw-diag.js doctor`)
and Python (`bin/ocdiag doctor` / `python3 -m ocdiag.doctor`) entry points
call this function. The Node entry is now a thin spawn wrapper.

Checks:
  - Python version (>= 3.8)
  - ocdiag package importable + version
  - All registered diag scripts respond to ``--help``
  - openclaw.json exists at expected path

Node version isn't visible from Python so we accept it as a passthrough
argument; if absent, doctor reports node check as ``skipped``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent


def _node_status(node_version: Optional[str]) -> dict:
    if not node_version:
        return {"version": None, "ok": True, "skipped": True,
                "reason": "Node check is performed by the Node entry; "
                          "ocdiag is fine without Node when invoked from Python"}
    # Normalize: accept "v22.22.2" or "22.22.2"
    normalized = node_version.lstrip("v")
    try:
        major = int(normalized.split(".", 1)[0])
    except ValueError:
        return {"version": normalized, "ok": False, "reason": "unparseable"}
    return {"version": normalized, "ok": major >= 18,
            "required": ">=18"}


def _python_status() -> dict:
    v = sys.version_info
    return {
        "version": f"{v.major}.{v.minor}.{v.micro}",
        "ok": v >= (3, 8),
        "required": ">=3.8",
        "executable": sys.executable,
    }


def _ocdiag_status() -> dict:
    try:
        import ocdiag  # type: ignore
        return {"ok": True, "version": getattr(ocdiag, "__version__", "?")}
    except ImportError as e:
        return {"ok": False, "error": str(e)[:200]}


def _diag_scripts_status() -> dict:
    from ocdiag.dispatcher import STATE_COLLECTORS, OBJECT_INSPECTORS
    failed = []
    all_scripts = []
    for mid, _label, rel in (*STATE_COLLECTORS, *OBJECT_INSPECTORS):
        all_scripts.append((mid, REPO_ROOT / rel))
    for mid, path in all_scripts:
        if not path.is_file():
            failed.append({"script": mid, "reason": "missing", "path": str(path)})
            continue
        r = subprocess.run(
            [sys.executable, str(path), "--help"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if r.returncode != 0:
            failed.append({
                "script": mid,
                "rc": r.returncode,
                "stderr": (r.stderr or "")[:200],
            })
    return {"ok": not failed, "total": len(all_scripts), "failed": failed}


def _openclaw_config_status() -> dict:
    home = os.path.expanduser("~")
    cfg = os.environ.get("OPENCLAW_CONFIG") or os.path.join(
        os.environ.get("OPENCLAW_HOME", os.path.join(home, ".openclaw")),
        "openclaw.json",
    )
    return {"path": cfg, "exists": os.path.isfile(cfg)}


def run(json_mode: bool = False, node_version: Optional[str] = None) -> int:
    """Execute the doctor check. Returns rc (0 if everything OK, 1 otherwise)."""
    result = {
        "node": _node_status(node_version),
        "python": _python_status(),
        "ocdiag": _ocdiag_status(),
        "diag_scripts": _diag_scripts_status(),
        "openclaw_config": _openclaw_config_status(),
    }

    ok = (
        result["node"].get("ok", True)
        and result["python"]["ok"]
        and result["ocdiag"]["ok"]
        and result["diag_scripts"]["ok"]
    )

    if json_mode:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        node = result["node"]
        if node.get("skipped"):
            print(f"ℹ Node check skipped (run via npx to verify Node version)")
        elif node["ok"]:
            print(f"✓ Node v{node['version']}")
        else:
            print(f"✗ Node v{node.get('version','?')} (need {node.get('required','?')})")

        py = result["python"]
        mark = "✓" if py["ok"] else "✗"
        print(f"{mark} Python {py['version']} ({py['executable']})")

        oc = result["ocdiag"]
        if oc["ok"]:
            print(f"✓ ocdiag package importable (version {oc['version']})")
        else:
            print(f"✗ ocdiag package not importable: {oc.get('error','?')}")

        ds = result["diag_scripts"]
        if ds["ok"]:
            print(f"✓ All {ds['total']} diagnostics respond to --help")
        else:
            print(f"✗ {len(ds['failed'])}/{ds['total']} diagnostics failed --help:")
            for f in ds["failed"]:
                print(f"    {f.get('script','?')} (rc={f.get('rc','?')})")

        cfg = result["openclaw_config"]
        if cfg["exists"]:
            print(f"✓ OpenClaw config present ({cfg['path']})")
        else:
            print(f"ℹ OpenClaw config not found ({cfg['path']}) — diagnostics will run but report missing")

    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ocdiag-doctor",
                                 description="Health-check the ocdiag install + environment")
    p.add_argument("--json", action="store_true", help="Emit JSON output")
    p.add_argument("--node-version", default=None,
                   help="Node version string (e.g. '20.12.1') passed in by the Node "
                        "shell. Omit when running from Python directly.")
    args = p.parse_args(argv)
    return run(json_mode=args.json, node_version=args.node_version)


if __name__ == "__main__":
    sys.exit(main())
