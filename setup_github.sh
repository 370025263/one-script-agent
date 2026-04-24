#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo ""
echo "=== GitHub 推送向导 ==="
echo ""

# ---------- 收集参数 ----------
read -rp "仓库名 [agent-learn]: " REPO_NAME
REPO_NAME="${REPO_NAME:-agent-learn}"

read -rp "Public 还是 Private? [public/private, 默认 public]: " VISIBILITY
VISIBILITY="${VISIBILITY:-public}"

read -rp "git user.name（你的姓名）: " GIT_NAME
read -rp "git user.email（你的邮箱）: " GIT_EMAIL

echo ""

# ---------- gh 登录 ----------
if ! gh auth status &>/dev/null; then
  echo ">>> gh 未登录，开始授权..."
  gh auth login
  echo ""
fi

# ---------- git 身份 ----------
git config --global user.name  "$GIT_NAME"
git config --global user.email "$GIT_EMAIL"
echo ">>> git 身份已设置：$GIT_NAME <$GIT_EMAIL>"

# ---------- 初始化仓库 ----------
if [[ ! -d ".git" ]]; then
  git init
  echo ">>> git init 完成"
fi

git add README.md main.py main_minimal.py main_improved.py run.sh .env.example .gitignore helloworld.py
git commit -m "init: coding agent 学习三部曲 (草稿→最小可跑→生产化)"
echo ">>> 首次 commit 完成"

# ---------- 建 GitHub 仓库并推送 ----------
gh repo create "$REPO_NAME" \
  --"$VISIBILITY" \
  --source=. \
  --remote=origin \
  --push

echo ""
echo "=== 推送完成 ==="
gh repo view "$REPO_NAME" --web 2>/dev/null || true
echo "仓库地址：https://github.com/$(gh api user -q .login)/$REPO_NAME"
