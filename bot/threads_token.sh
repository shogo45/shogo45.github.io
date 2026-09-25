#!/usr/bin/env bash
# Threadsのトークンを取り出して GITHUB_ENV に入れる（ログには出さない）。
# 延長したトークンは bot/data/threads_token.enc に暗号化して保存している（鍵は Secrets の THREADS_KEY）。
# 暗号化ファイルが無い／読めないときは Secrets の THREADS_TOKEN（最初に手で登録したもの）を使う。
set -euo pipefail
tok=""
if [ -f bot/data/threads_token.enc ] && [ -n "${THREADS_KEY:-}" ]; then
  tok=$(openssl enc -d -aes-256-cbc -pbkdf2 -a -A -pass env:THREADS_KEY -in bot/data/threads_token.enc 2>/dev/null || true)
fi
[ -z "$tok" ] && tok="${THREADS_TOKEN_SECRET:-}"
[ -n "$tok" ] && echo "::add-mask::$tok"
echo "THREADS_TOKEN=$tok" >> "$GITHUB_ENV"
