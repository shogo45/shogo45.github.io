"""GitHub Actions から呼ばれ、時刻が来た投稿を Bluesky に1件だけ出す（Macの電源に関係なく動く）。

- 認証：環境変数 BSKY_HANDLE / BSKY_APP_PASSWORD（GitHub Secrets）
- キュー：OUT_DIR/queue_YYYY-MM-DD.json（毎朝 daily.yml の run_daily.py が作る）
- 取りこぼした古い枠はまとめて出さず skipped にする。失敗したら終了コード1（Actionsの失敗通知が届く）
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import bsky

OUT = Path(os.environ.get("OUT_DIR") or Path(__file__).resolve().parent / "out").resolve()


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    qpath = OUT / f"queue_{today}.json"
    if not qpath.exists():
        print("今日のキューがありません"); return 0
    queue = json.loads(qpath.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%H:%M")
    due = [q for q in queue if not q.get("bsky") and q["time"] <= now]
    if not due:
        print("投稿する枠はありません"); return 0
    for old in due[:-1]:
        old["bsky"] = "skipped"
    q = due[-1]
    code = 0
    try:
        q["bsky"] = bsky.post(q["text"], os.environ["BSKY_HANDLE"], os.environ["BSKY_APP_PASSWORD"])
        print(f"✅ Bluesky {q['time']} {q['bsky']}")
    except Exception as e:
        q["bsky"] = "failed"
        print(f"❌ Bluesky投稿失敗 {q['time']}: {e}")
        code = 1
    qpath.write_text(json.dumps(queue, ensure_ascii=False, indent=1), encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
