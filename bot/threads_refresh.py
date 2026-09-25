"""Threads の長期トークン（60日）を延長する（GitHub Actions から週1回）。

- 延長のAPIは、多くの場合「同じトークンの期限を延ばす」だけ。同じ文字列が返れば、Secrets の更新は不要。
- 返ってきたトークンは bot/data/threads_token.enc に暗号化して保存する（鍵は Secrets の THREADS_KEY。リポジトリは公開なので平文では置かない）。
  post.yml も threads_token.sh でこのファイルを優先して使う。
- トークン自体はログに出さない。発行から24時間以内は延長できない（そのときはスキップ）。
"""
import hashlib
import os
import sys
import urllib.error

import threads


def main():
    tok = os.environ.get("THREADS_TOKEN", "")
    if not tok:
        print("THREADS_TOKEN が未設定"); return 0
    try:
        new = threads.refresh(tok)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        if "24 hours" in body or "too early" in body.lower():
            print("発行から24時間以内なので、延長は次回に"); return 0
        print(f"❌ 延長に失敗: {e.code} {body}"); return 1
    if not os.environ.get("THREADS_KEY"):
        print("❌ THREADS_KEY（暗号化の鍵）が未登録なので、延長したトークンを保存できません"); return 1
    # 新しいトークンを暗号化して保存（鍵は Secrets の THREADS_KEY）。次からはこれを使う
    import subprocess
    subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-a", "-A", "-salt", "-pass", "env:THREADS_KEY",
                    "-out", "bot/data/threads_token.enc"], input=new.encode(), check=True)
    print("✅ Threadsトークンを延長して保存しました（" + ("同じトークン" if new == tok else "新しいトークン") + "）")
    return 0

if __name__ == "__main__":
    sys.exit(main())
