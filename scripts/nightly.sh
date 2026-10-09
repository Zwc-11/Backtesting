#!/usr/bin/env bash
set -u -o pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir" || exit 2
export XASSET_DATA_DIR="${XASSET_DATA_DIR:-$repo_dir/data}"
mkdir -p "$XASSET_DATA_DIR"
status=0
"$repo_dir/.venv/bin/xasset" record --days 7 || status=$?
"$repo_dir/.venv/bin/xasset" health --sessions 5 \
  --output "$XASSET_DATA_DIR/health-latest.json" || status=$?
"$repo_dir/.venv/bin/xasset" audit \
  --output "$XASSET_DATA_DIR/quality-latest.json" || status=$?
exit "$status"
