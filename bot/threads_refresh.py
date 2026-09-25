"""Threads の長期トークン（60日）を延長する（GitHub Actions から週1回）。

- 延長のAPIは、多くの場合「同じトークンの期限を延ばす」だけ。同じ文字列が返れば、Secrets の更新は不要。
- 違う文字列が返った場合は Secrets を書き換える必要があるが、Actions の標準トークンでは書けないので、
  失敗にして通知する（そのときは Meta の画面で再発行→ pbpaste | gh secret set THREADS_TOKEN）。
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
    if new == tok:
        print("✅ Threadsトークンの期限を延長しました（同じトークン・Secretsの更新は不要）"); return 0
    print("❌ 延長で新しいトークンが返りました。Secrets の更新が必要です（Meta の画面で再発行して登録し直してください）",
          hashlib.sha256(new.encode()).hexdigest()[:8])
    return 1


if __name__ == "__main__":
    sys.exit(main())
