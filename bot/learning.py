"""投稿の成果から「どのジャンル・時間帯・書き出しの型が稼げるか」を学習する。

材料
- data/posted_history.json : 投稿した商品（日付・item_code・genre・slot・hook_id）
- data/results.json        : 楽天アフィリエイトのレポートから取り込んだ成果
                             [{"date": "YYYY-MM-DD", "item_code" or "name": ..., "clicks": n, "orders": n, "reward": 円}]

出力
- data/weights.json : {"genre": {...}, "slot": {...}, "hook": {...}}  各値は 0.5〜2.0 の倍率（1.0＝平均）

計算：報酬（無ければ成約×50円、それも無ければクリック×1円）を「投稿1件あたり」にし、
事前分布（全体平均を投稿3件ぶん）で縮めてから全体平均との比をとる。投稿が少ない項目が暴れないようにするため。
"""
import json
import os
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR") or BASE / "data").resolve()
PRIOR_POSTS = 3


def _load(name, default):
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def value(r):
    return r.get("reward") or r.get("orders", 0) * 50 or r.get("clicks", 0) * 1


def update():
    posts = [p for p in _load("posted_history.json", []) if p.get("item_code")]   # リンクなし投稿は除外
    results = _load("results.json", [])
    if not posts:
        return {}

    by_code = defaultdict(float)
    for r in results:
        by_code[r.get("item_code") or r.get("name", "")] += value(r)

    total = sum(by_code[p["item_code"]] for p in posts)
    mean = total / len(posts) if posts else 0
    weights = {}
    for dim in ("genre", "slot", "hook_id"):
        agg = defaultdict(lambda: [0.0, 0])
        for p in posts:
            if dim in p:
                agg[p[dim]][0] += by_code[p["item_code"]]
                agg[p[dim]][1] += 1
        w = {}
        for k, (v, n) in agg.items():
            shrunk = (v + mean * PRIOR_POSTS) / (n + PRIOR_POSTS)
            w[k] = round(min(2.0, max(0.5, shrunk / mean)), 3) if mean > 0 else 1.0
        weights[dim] = w
    (DATA / "weights.json").write_text(json.dumps(weights, ensure_ascii=False, indent=1), encoding="utf-8")
    return weights


def load_weights():
    return _load("weights.json", {})


if __name__ == "__main__":
    print(json.dumps(update(), ensure_ascii=False, indent=1))
