"""Threads（Meta 公式の Threads API）へ投稿する。無料。

- 認証：長期アクセストークン（60日有効）。GitHub Secrets の THREADS_TOKEN
- 投稿は2段階：①コンテナを作る（/me/threads）→ ②公開する（/me/threads_publish）
- 本文は500文字まで。URLは自動でリンクになる
"""
import json
import time
import urllib.parse
import urllib.request

API = "https://graph.threads.net/v1.0"


def _post(path, params):
    req = urllib.request.Request(f"{API}/{path}", data=urllib.parse.urlencode(params).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def post(text, token, image=None):
    """image（公開されている画像URL）があれば画像付きで投稿。画像で失敗したら文章だけで出す。"""
    text = text if len(text) <= 500 else text[:499] + "…"
    c = None
    if image:
        try:
            c = _post("me/threads", {"media_type": "IMAGE", "image_url": image, "text": text, "access_token": token})
            time.sleep(15)   # 画像の取り込みを待つ
        except Exception as e:
            print(f"   画像なしで投稿（{e}）")
            c = None
    if not c:
        c = _post("me/threads", {"media_type": "TEXT", "text": text, "access_token": token})
        time.sleep(3)   # コンテナの準備を待つ（公式の推奨）
    p = _post("me/threads_publish", {"creation_id": c["id"], "access_token": token})
    return p["id"]


def refresh(token):
    """長期トークンを延長する（発行から24時間以降・期限切れ前に呼ぶ）。新しいトークンを返す。"""
    q = urllib.parse.urlencode({"grant_type": "th_refresh_token", "access_token": token})
    with urllib.request.urlopen(f"https://graph.threads.net/refresh_access_token?{q}", timeout=30) as r:
        return json.load(r)["access_token"]


def whoami(token):
    """トークンが有効か確かめる（ユーザー名を返す）。"""
    q = urllib.parse.urlencode({"fields": "username", "access_token": token})
    with urllib.request.urlopen(f"{API}/me?{q}", timeout=30) as r:
        return json.load(r)["username"]
