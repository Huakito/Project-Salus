#!/usr/bin/env bash
set -euo pipefail

payload=$(cat)
stop_active=$(printf '%s' "$payload" | jq -r '.stop_hook_active // false')
[ "$stop_active" = "true" ] && exit 0

project_dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"

# Only run pytest if Python files or commands.json have uncommitted modifications.
changed=$(git -C "$project_dir" status --porcelain -- 'webapp/' 'commands.json' 'firmware/*.py' 2>/dev/null | grep -E '\.(py|json)$' || true)
[ -z "$changed" ] && exit 0

py="$project_dir/.venv/bin/python"
if [ ! -x "$py" ]; then
  common_git=$(git -C "$project_dir" rev-parse --git-common-dir 2>/dev/null || true)
  if [ -n "$common_git" ] && [ -x "$(dirname "$common_git")/.venv/bin/python" ]; then
    py="$(dirname "$common_git")/.venv/bin/python"
  else
    exit 0
  fi
fi

if ! output=$(cd "$project_dir/webapp" && "$py" -m pytest -q 2>&1); then
  echo "Tests failed in webapp/ — fix before stopping:" >&2
  printf '%s\n' "$output" | tail -n 30 >&2
  exit 2
fi
