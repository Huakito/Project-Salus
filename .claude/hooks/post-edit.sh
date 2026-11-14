#!/usr/bin/env bash
set -euo pipefail

file=$(jq -r '.tool_input.file_path // empty')
[ -z "$file" ] || [ ! -f "$file" ] && exit 0

project_dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"
py="$project_dir/.venv/bin/python"
if [ ! -x "$py" ]; then
  common_git=$(git -C "$project_dir" rev-parse --git-common-dir 2>/dev/null || true)
  if [ -n "$common_git" ] && [ -x "$(dirname "$common_git")/.venv/bin/python" ]; then
    py="$(dirname "$common_git")/.venv/bin/python"
  else
    py="python3"
  fi
fi

case "$file" in
  *.py)
    "$py" -m py_compile "$file" 2>&1 || exit 2
    if command -v ruff >/dev/null 2>&1; then
      ruff format "$file" >/dev/null 2>&1 || true
      ruff check "$file" >&2 || exit 2
    fi
    ;;
  *.js|*.mjs)
    if command -v node >/dev/null 2>&1; then
      node --check "$file" >&2 || exit 2
    fi
    ;;
  *.json)
    jq empty "$file" >&2 || exit 2
    ;;
esac
