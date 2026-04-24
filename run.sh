#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 优先加载 ~/.aikey，其次 .env
[[ -f ~/.aikey ]] && set -a && . ~/.aikey && set +a
[[ -f "$ROOT/.env" ]] && set -a && . "$ROOT/.env" && set +a

export OPENAI_API_KEY="${DEEPSEEK_API_KEY:?需要在 ~/.aikey 里设置 DEEPSEEK_API_KEY}"
export OPENAI_BASE_URL="${DEEPSEEK_BASE_URL_OPENAI:-https://api.deepseek.com}"

exec python3.11 "$ROOT/main_improved.py" "$@"
