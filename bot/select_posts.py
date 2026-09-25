"""その日の投稿案から2〜3件を自動で選び、見られやすい時間に割り当てる。

選び方（点数の高い順・ジャンルは重複させない）
- レビュー件数が多い（ハズレにくい＝クリック後に買われやすい）
- ポイント倍率の締め切りがある（今買う理由がある）
- 消耗品・美容・健康（リピートされやすく成約しやすい）
- 急上昇・初登場（話題性）

出力: out/queue_YYYY-MM-DD.json  [{"time": "12:10", "text": "...", "item_code": "...", "posted": false}, ...]
"""
import json
import math
import os
import random
from datetime import datetime
from pathlib import Path

from learning import load_weights

BASE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("OUT_DIR") or BASE / "out").resolve()
DATA = Path(os.environ.get("DATA_DIR") or BASE / "data").resolve()

# 調査（research/tactics.md）より：主戦場は21:00〜22:30、補助で昼。投稿間隔は90分以上
SLOTS = ["21:00", "22:30"]   # 商品リンク投稿（最後の行に単独で「【PR】楽天アフィリエイト」）
SITE_SLOT = "12:10"           # 昼は商品を名指ししない「サイト更新」投稿（自分の最安値ページへ誘導。ページ冒頭にPR表記あり）
SITE_URL = "https://totalyamanote.github.io/"
REPEAT_GENRES = {"日用消耗品", "美容・コスメ", "ダイエット・健康"}
NO_REPEAT_DAYS = 14   # 同じ商品は2週間は投稿しない


def score(c):
    s = math.log10(c["review_count"] + 1) * 10          # レビュー1万件で40点
    s += 8 if c["review_avg"] >= 4.5 else 0
    s += 15 if c.get("point_end") else 0                 # 締め切りあり
    s += min(c["point_rate"], 20)                         # 倍率そのもの
    s += 12 if c["genre"] in REPEAT_GENRES else 0
    s += 10 if any(("急上昇" in r or "初登場" in r) for r in c["reasons"]) else 0
    # 1件売れたときの報酬（楽天の上限は1商品1,000円）。料率アップ商品ほど有利
    reward = min(c["price"] * c.get("affiliate_rate", 0) / 100, 1000)
    s += min(reward / 20, 20)                             # 400円で20点が上限
    if c["review_count"] < 30:
        s -= 30                                           # 実績が薄いものは避ける
    return s


def pick(candidates, n=3):
    """上位2件は点数×学習倍率で選び、最後の1件は探索枠（まだ試していないジャンルを優先）。"""
    w = load_weights()
    history = load_history()
    recent = {h["item_code"] for h in history if (datetime.now() - datetime.fromisoformat(h["date"])).days < NO_REPEAT_DAYS}
    gw = w.get("genre", {})
    ranked = sorted(candidates, key=lambda c: score(c) * gw.get(c["genre"], 1.0), reverse=True)
    ranked = [c for c in ranked if c["item_code"] not in recent]
    chosen, genres = [], set()
    for c in ranked:
        if c["genre"] in genres:
            continue
        chosen.append(c)
        genres.add(c["genre"])
        if len(chosen) == n - 1:
            break
    # 探索枠：投稿回数の少ないジャンルから、上位10件の中でランダムに
    tried = {}
    for h in history:
        tried[h.get("genre")] = tried.get(h.get("genre"), 0) + 1
    pool = [c for c in ranked[:10] if c["genre"] not in genres and score(c) > 0]
    if pool:
        pool.sort(key=lambda c: tried.get(c["genre"], 0))
        least = tried.get(pool[0]["genre"], 0)
        chosen.append(random.choice([c for c in pool if tried.get(c["genre"], 0) == least]))
    return chosen


def load_history():
    try:
        return json.loads((DATA / "posted_history.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


TEXT_SLOTS = ["07:40", "18:30"]   # 通勤の行き帰り（閲覧の山）。リンクなし投稿（宣伝アカウントに見えないように、リンク投稿の間にはさむ）


def text_posts(candidates, results, today):
    """リンクなしの情報投稿を2件作る。データにあることだけ書く。"""
    d = datetime.strptime(today, "%Y-%m-%d")
    posts = []
    # 朝：ジャンル別ランキング1位
    from rakuten import load_config
    cfg = load_config(BASE)
    tops = {}
    for g in cfg["ranking_genres"]:
        try:
            items = json.loads((DATA / f"ranking_{g['genre_id']}.json").read_text(encoding="utf-8"))["items"]
            tops[g["name"]] = next(i for i in items if i["rank"] == 1)
        except (FileNotFoundError, StopIteration, KeyError, json.JSONDecodeError):
            pass
    if tops:
        from run_daily import short_name
        lines = [f"おはようございます☀ {d.month}/{d.day}の楽天ランキング1位まとめ", ""]
        for g, c in list(tops.items())[:5]:
            lines.append(f"・{g}：{short_name(c['name'], 22)}")
        lines += ["", "気になるものは夜に詳しく紹介します"]
        posts.append("\n".join(lines))
    # 夕方：ポイント締め切り or 最安値まとめ
    recent = {h.get("item_code") for h in load_history()
              if (d - datetime.fromisoformat(h["date"])).days < NO_REPEAT_DAYS}
    ends = [c for c in candidates if c.get("point_end") and c["point_rate"] >= 5
            and c["item_code"] not in recent
            and datetime.strptime(c["point_end"][:16], "%Y-%m-%d %H:%M") > datetime.now()]
    if ends:
        from run_daily import short_name
        lines = ["今やってるポイントアップ、締め切りが近い順", ""]
        for c in sorted(ends, key=lambda c: c["point_end"])[:4]:
            e = datetime.strptime(c["point_end"][:16], "%Y-%m-%d %H:%M")
            lines.append(f"・{short_name(c['name'], 18)}　{c['point_rate']}倍（{e.month}/{e.day}まで）")
        lines += ["", "買う予定のものがあれば、倍率が高いうちに🛒"]
        posts.append("\n".join(lines))
    elif results:
        lines = ["今日の楽天『実質最安値』（価格−ポイント）", ""]
        for r in results:
            if r["items"]:
                lines.append(f"・{r['name']}：¥{r['items'][0]['effective']:,}" + ("📉最安更新" if r["is_new_low"] else ""))
        lines += ["", "毎朝更新してます。一覧はプロフのリンクから"]
        posts.append("\n".join(lines))
    return [{"time": t, "item_code": None, "genre": "情報", "hook_id": "text", "text": x,
             "has_link": False, "posted": False} for t, x in zip(TEXT_SLOTS, posts)]


def site_post(results, today):
    """特定の商品を勧めない、最安値ページの更新のお知らせ。ジャンル名と実質価格だけ書く（データにあることだけ）。"""
    rows = [r for r in results if r.get("items")]
    if not rows:
        return []
    d = datetime.strptime(today, "%Y-%m-%d")
    lines = [f"{d.month}/{d.day}の楽天『実質最安値』ウォッチ更新しました（価格−ポイントで毎朝比較）", ""]
    for r in rows[:6]:
        lines.append(f"・{r['name']}：¥{r['items'][0]['effective']:,}" + ("📉最安更新" if r.get("is_new_low") else ""))
    lines += ["", f"一覧はこちら → {SITE_URL}", "※リンク先のページには広告（PR）を含みます"]
    return [{"time": SITE_SLOT, "item_code": None, "genre": "サイト", "hook_id": "site", "text": "\n".join(lines),
             "has_link": True, "posted": False}]


def build_queue(candidates, texts, today, results=None):
    by_code = dict(zip([c["item_code"] for c in candidates], texts))
    chosen = pick(candidates, n=len(SLOTS))
    from run_daily import compose_post
    stamp = datetime.now().strftime("%Y/%m/%d %H:%M")
    used = set()
    for c in chosen:
        for v in range(3):
            c["_variant"] = v
            text = compose_post(c, stamp)
            if c.get("hook_id") not in used:
                break
        used.add(c.get("hook_id"))
        by_code[c["item_code"]] = text
    # 成果の良い時間帯に点数の高い商品を割り当てる
    sw = load_weights().get("slot", {})
    slots = sorted(SLOTS, key=lambda t: sw.get(t, 1.0), reverse=True)
    queue = [{"time": t, "item_code": c["item_code"], "genre": c["genre"], "hook_id": c.get("hook_id", ""),
              "text": by_code[c["item_code"]], "has_link": True, "posted": False}
             for t, c in zip(slots, chosen)]
    queue += text_posts(candidates, results or [], today)
    queue += site_post(results or [], today)
    queue.sort(key=lambda q: q["time"])
    # 同じ日に作り直しても、すでに投稿済みの枠は二重に出さない（投稿済みの印を引き継ぐ）
    try:
        done = {q["time"]: q for q in json.loads((OUT / f"queue_{today}.json").read_text(encoding="utf-8"))}
    except (FileNotFoundError, json.JSONDecodeError):
        done = {}
    for q in queue:
        for k in ("bsky", "posted"):
            if done.get(q["time"], {}).get(k):
                q[k] = done[q["time"]][k]
    (OUT / f"queue_{today}.json").write_text(json.dumps(queue, ensure_ascii=False, indent=1), encoding="utf-8")
    return queue
