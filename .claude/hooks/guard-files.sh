#!/usr/bin/env bash
set -euo pipefail

file=$(jq -r '.tool_input.file_path // empty')
[ -z "$file" ] && exit 0

case "$file" in
  *.env.example) exit 0 ;;
  *.env|*.env.*|*secrets*|*credentials*|*.db|*.db-*|*.sqlite|*.sqlite3|recordings/*|*/recordings/*|webapp/data/*|*/webapp/data/*|exports/*|*/exports/*)
    echo "Blocked edit to protected/patient-data path: $file" >&2
    exit 2
    ;;
  *package-lock.json|*pnpm-lock.yaml|*yarn.lock|*Cargo.lock|*uv.lock|*poetry.lock)
    echo "Blocked direct edit to lockfile: $file" >&2
    exit 2
    ;;
esac
