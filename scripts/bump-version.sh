#!/usr/bin/env bash
# Bump the version string in all three places that must agree:
#   package.json:version
#   pyproject.toml:version
#   ocdiag/__init__.py:__version__
#
# Usage:  scripts/bump-version.sh 0.2.0
#
# Verifies after editing — fails noisy if any file ends up out of sync.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <new-version>" >&2
  exit 64
fi

NEW="$1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# package.json — line "version": "0.1.x"
python3 -c "
import json, sys
p = '$ROOT/package.json'
d = json.load(open(p))
d['version'] = '$NEW'
open(p, 'w').write(json.dumps(d, indent=2) + '\n')
"

# pyproject.toml — top-level [project] version = "0.1.x"
sed -i.bak -E "s/^version = \".*\"\$/version = \"$NEW\"/" "$ROOT/pyproject.toml"
rm -f "$ROOT/pyproject.toml.bak"

# ocdiag/__init__.py — __version__ = "0.1.x"
sed -i.bak -E "s/^__version__ = \".*\"\$/__version__ = \"$NEW\"/" "$ROOT/ocdiag/__init__.py"
rm -f "$ROOT/ocdiag/__init__.py.bak"

# Verify
PKG=$(python3 -c "import json; print(json.load(open('$ROOT/package.json'))['version'])")
PYP=$(grep -E '^version' "$ROOT/pyproject.toml" | cut -d'"' -f2)
INI=$(grep -E '^__version__' "$ROOT/ocdiag/__init__.py" | cut -d'"' -f2)

echo "package.json   = $PKG"
echo "pyproject.toml = $PYP"
echo "ocdiag/__init__= $INI"

if [[ "$PKG" != "$NEW" || "$PYP" != "$NEW" || "$INI" != "$NEW" ]]; then
  echo "ERROR: versions out of sync after bump" >&2
  exit 1
fi
echo "✓ all three at $NEW"
