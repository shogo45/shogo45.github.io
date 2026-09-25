"""二重投稿を防ぐ「先取り」。投稿する前に、キューに「投稿中」の印を付けて GitHub に push する。
同時に2つ動いても、push が通るのは先に着いた方だけ（後の方は push が弾かれる）ので、弾かれた方は投稿しない。
"""
import os
import subprocess


def _git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True)


def claim(path, msg):
    """path（キューのファイル）を commit して push。成功すれば True（この実行が投稿してよい）。"""
    if not os.environ.get("GITHUB_ACTIONS"):
        return True   # 手元で動かすときは先取りしない
    _git("config", "user.name", "github-actions[bot]")
    _git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    _git("add", str(path))
    if _git("commit", "-qm", msg).returncode != 0:
        return False
    r = _git("push", "-q", "origin", "HEAD:main")   # 取り込み（pull）はしない：先に誰かが push していたら失敗させる
    return r.returncode == 0
