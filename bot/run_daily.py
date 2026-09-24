"""毎朝1回実行する。

① ランキング速報：前日との差分（初ランクイン／急上昇／ポイント高倍率）から X/Threads 用の投稿案を作る
③ 最安値ウォッチ：watchlist の実質最安値（価格−ポイント）を記録し、静的HTMLページを生成する

使い方:
  python3 run_daily.py --mock   # キー無しで動作確認
  python3 run_daily.py          # 本番（config.json にキーを入れてから）
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import zlib
from datetime import datetime, timedelta
from pathlib import Path

from rakuten import Rakuten, load_config
from select_posts import build_queue

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR") or BASE / "data").resolve()
OUT = Path(os.environ.get("OUT_DIR") or BASE / "out").resolve()
SITE = Path(os.environ.get("SITE_DIR") or BASE / "site").resolve()
HISTORY_DAYS = 90


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def short_name(name, limit=38):
    # 楽天の商品名は【】や送料無料などの宣伝文句が長いので落とす
    s = re.sub(r"【[^】]*】|\[[^\]]*\]|＼[^／]*／|★|☆|◆|♪", " ", name)
    s = re.sub(r"(送料無料|ポイント\d+倍|楽天\d+位|あす楽|クーポン\S*|P\d+倍)", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def x_weight(text):
    """Xの文字数（全角=2, 半角=1, URL=23）。上限280。"""
    urls = re.findall(r"https?://\S+", text)
    rest = re.sub(r"https?://\S+", "", text)
    return sum(1 if ord(c) < 0x1100 else 2 for c in rest) + 23 * len(urls)


# ---------------- ① ランキング速報 ----------------
def ranking_digest(api, cfg, today):
    rules = cfg["ranking_rules"]
    posts = []
    for g in cfg["ranking_genres"]:
        snap_path = DATA / f"ranking_{g['genre_id']}.json"
        prev = load_json(snap_path, {})
        prev_rank = {x["item_code"]: x["rank"] for x in prev.get("items", [])}
        try:
            items = api.ranking(g["genre_id"])
        except RuntimeError as e:
            print(f"   ⚠ {g['name']} のランキング取得に失敗: {e}", file=sys.stderr)
            continue
        first_run = not prev_rank

        for it in items:
            code, rank = it["item_code"], it["rank"]
            reasons = []
            if not first_run and code not in prev_rank and rank <= 30:
                reasons.append(f"🆕 {g['name']}ランキング初登場{rank}位")
            elif code in prev_rank and prev_rank[code] - rank >= rules["jump_threshold"]:
                reasons.append(f"📈 {g['name']}で{prev_rank[code]}位→{rank}位に急上昇")
            if it["point_rate"] >= rules["high_point_rate"]:
                reasons.append(f"💰 ポイント{it['point_rate']}倍")
            if first_run and rank <= 3:
                reasons.append(f"👑 {g['name']}ランキング{rank}位")
            if reasons:
                posts.append({"genre": g["name"], "reasons": reasons, **it})

        save_json(snap_path, {"date": today, "items": items})

    # 急上昇・初登場 > ポイント、の順で上位だけ
    posts.sort(key=lambda p: (not any(r[0] in "🆕📈👑" for r in p["reasons"]), p["rank"]))
    return posts[: rules["max_posts"]]


def _review_phrase(n):
    if n >= 10000:
        return f"レビュー{n/10000:.1f}万件".replace(".0万", "万")
    if n >= 1000:
        return f"レビュー{n:,}件"
    return ""


def _hook(p):
    """事実（順位・ポイント・レビュー数）から書き出しを作る。使用体験の捏造はしない。
    どの型を使ったかを p["hook_id"] に残し、learning.py で成果と突き合わせる。"""
    text = _hook_text(p)
    return text


def _hook_text(p):
    r = " ".join(p["reasons"])
    rv = _review_phrase(p["review_count"])
    kind = short_name(p["name"], 18)
    pick = (zlib.crc32(p["item_code"].encode()) + p.get("_variant", 0)) % 3
    p["hook_id"] = f"jump{pick}"
    if "急上昇" in r:
        m = re.search(r"(\d+)位→(\d+)位", r)
        return [f"昨日{m.group(1)}位だったのに今日{m.group(2)}位まで来てる📈 何かあった？",
                f"{kind}、1日で{m.group(1)}位→{m.group(2)}位。急に売れ始めてて気になる",
                f"ランキング見てたら{m.group(2)}位に急浮上してるのがあった👀"][pick]
    if "初登場" in r:
        p["hook_id"] = f"new{pick}"
        m = re.search(r"初登場(\d+)位", r)
        return [f"今日いきなり{p['genre']}ランキング{m.group(1)}位に入ってきたやつ",
                f"見慣れないのが{m.group(1)}位に初登場してた👀",
                f"{p['genre']}ランキング、新顔が{m.group(1)}位に"][pick]
    if "ランキング" in r and "位" in r:
        p["hook_id"] = f"rank{pick}"
        m = re.search(r"ランキング(\d+)位", r)
        return [f"{p['genre']}で今{m.group(1)}位のやつ、{rv or '評価' }{'もすごい' if rv else '高め'}",
                f"{p['genre']}ランキング{m.group(1)}位。{rv + 'は強い' if rv else '売れてる'}",
                f"今{p['genre']}で一番売れてる系のやつ（{m.group(1)}位）"][pick]
    pt = re.search(r"ポイント(\d+)倍", r)
    if pt:
        p["hook_id"] = f"point{pick}"
        base = f"{rv}の" if rv else ""
        return [f"{base}{kind}が今ポイント{pt.group(1)}倍になってた👀",
                f"ポイント{pt.group(1)}倍きてる。{rv + 'の定番' if rv else '買う予定あった人向け'}",
                f"これポイント{pt.group(1)}倍なの地味にありがたい"][pick]
    return f"{kind}、気になってる人向けにメモ"


def _comment(p):
    price = p["price"]
    pick = (zlib.crc32((p["item_code"] + "c").encode()) + p.get("_variant", 0)) % 3
    if p["point_rate"] >= 5:
        soon = False
        if p.get("point_end"):
            try:
                soon = (datetime.strptime(p["point_end"][:16], "%Y-%m-%d %H:%M") - datetime.now()).days < 7
            except ValueError:
                pass
        consumable = p.get("genre") in ("日用消耗品", "美容・コスメ", "ダイエット・健康")
        last = ("倍率はすぐ戻ることが多いので、買う予定だった人は今のうちかも。" if soon
                else "消耗品なら、倍率が高いうちにストックしておくのもあり。" if consumable
                else "買い替えを考えてた人は、倍率が高いうちにチェックを。")
        return ["消耗品や買い替え予定のものは、ポイント高い日にまとめるのが一番おトク。",
                f"実質{price * (100 - p['point_rate']) // 100:,}円くらいの計算。",
                last][pick]
    if p["review_count"] >= 1000:
        return ["これだけレビューが集まってると、ハズレにくいのはありがたい。",
                "迷ったときは結局こういう定番に落ち着く。",
                "レビューを読んでから決めたい派なので、件数が多いのは助かる。"][pick]
    return ["気になった人はレビューだけでも見てみて。",
            "まだレビューは少なめなので、買うなら詳細を要チェック。",
            "価格は変わりやすいので、気になる人は早めに確認を。"][pick]


def compose_post(p, stamp):
    """先頭に「PR｜」（短く、でも必ず先頭。ステマ規制のため末尾やタグには埋めない）、事実＋ひとこと＋締め切り＋ハッシュタグ。自分で使った体験は書かない。"""
    short_stamp = datetime.strptime(stamp, "%Y/%m/%d %H:%M").strftime("%-m/%-d %H:%M")
    deadline = ""
    if p.get("point_end") and p["point_rate"] >= 2:
        try:
            end = datetime.strptime(p["point_end"][:16], "%Y-%m-%d %H:%M")
            deadline = f"⏰ポイント{p['point_rate']}倍は{end.month}/{end.day} {end:%H:%M}まで"
        except ValueError:
            pass
    ship = " 送料無料" if p.get("postage_free") else ""
    tags = f"#楽天 {GENRE_TAGS.get(p.get('genre'), '#楽天市場')}"
    lines = ["PR｜" + _hook(p), "", _comment(p), "",
             *( [deadline] if deadline else [] ),
             f"{short_name(p['name'], 30)} ¥{p['price']:,}{ship}",
             p["url"], tags, f"※{short_stamp}時点"]
    text = "\n".join(lines)
    i = lines.index(p["url"]) - 1
    for limit in (24, 18, 12, 8):   # 商品名を段階的に短くする（必ず終わる）
        if x_weight(text) <= 280:
            break
        lines[i] = short_name(p["name"], limit) + f" ¥{p['price']:,}{ship}"
        text = "\n".join(lines)
    if x_weight(text) > 280:
        lines.pop(2); lines.pop(2)   # ひとことを落とす
        text = "\n".join(lines)
    return text


def refresh_points(api, posts, cfg):
    """最新の倍率で理由を作り直す。倍率が条件を下回った『ポイントだけ』の候補は外す。"""
    out = []
    for p in posts:
        d = api.item_detail(p["item_code"])
        if d:
            # 終了日時が過ぎた倍率は無効（APIが古い倍率を返すことがある）
            try:
                if d["point_end"] and datetime.strptime(d["point_end"][:16], "%Y-%m-%d %H:%M") < datetime.now():
                    d["point_rate"], d["point_end"] = 1, ""
            except ValueError:
                pass
            p.update(d)
            p["reasons"] = [r for r in p["reasons"] if "ポイント" not in r]
            if p["point_rate"] >= cfg["ranking_rules"]["high_point_rate"]:
                p["reasons"].append(f"💰 ポイント{p['point_rate']}倍")
        if p["reasons"]:
            out.append(p)
    return out


GENRE_TAGS = {
    "パソコン・周辺機器": "#ガジェット", "家電": "#家電", "美容・コスメ": "#スキンケア",
    "ダイエット・健康": "#健康", "日用消耗品": "#日用品",
}


# ---------------- ③ 最安値ウォッチ ----------------
def todays_watchlist(cfg, today):
    """固定の監視商品＋日替わりのカテゴリ（watchlist_pool から毎日 pool_per_day 個を順番に）。"""
    pool = cfg.get("watchlist_pool", [])
    n = cfg.get("pool_per_day", 0)
    if not pool or not n:
        return cfg["watchlist"]
    start = (datetime.strptime(today, "%Y-%m-%d").toordinal() * n) % len(pool)
    return cfg["watchlist"] + [pool[(start + i) % len(pool)] for i in range(n)]


COLOR_WORDS = r"(ブラック|ホワイト|白|黒|ピンク|ブルー|グレー|レッド|グリーン|ベージュ|パープル|紫|ネイビー|シルバー|ゴールド|イエロー|オレンジ|ブラウン|クリーム|色|カラー)"


def dedupe_key(it):
    """色違い・同じ商品の出品違いをまとめるためのキー（商品名の頭の部分から色の言葉と記号を除いたもの）。"""
    n = re.sub(COLOR_WORDS, "", short_name(it["name"], 80))
    n = re.sub(r"[\s　・/｜|()（）\[\]【】,，、。!！?？\-]", "", n)
    return n[:14]


def price_watch(api, cfg, today, exclude=None):
    """exclude：ほかの欄にすでに出している商品コード（項目をまたいだ重複を避ける）"""
    shown = set(exclude or [])
    hist_path = DATA / "price_history.json"
    hist = load_json(hist_path, {})
    cutoff = (datetime.now() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    results = []

    for w in todays_watchlist(cfg, today):
        # 安い順の上位はアクセサリーで埋まりがちなので、除外後に5件そろうまで最大3ページ見る
        # 美容・健康のように種類が多いものは sort=-reviewCount（人気順）で取り、その中で実質価格の安い順に並べる
        ng = w.get("ng_words", [])
        items = []
        for page in range(1, 4):
            batch = api.search_cheapest(w["keyword"], w.get("min_price"), w.get("max_price"), page=page,
                                        sort=w.get("sort", "+itemPrice"), genre_id=w.get("genre_id"))
            items += [it for it in batch
                      if not any(n in it["name"] for n in ng)
                      and it["review_count"] >= w.get("min_reviews", 0)]
            if len(items) >= 5 or len(batch) < 30:
                break
        # ポイントアップ中の商品も追加で探す（倍率が高いほど実質価格が下がるので、最安の候補になりうる）
        try:
            extra = api.search_cheapest(w["keyword"], w.get("min_price"), w.get("max_price"),
                                        sort="-reviewCount", genre_id=w.get("genre_id"), point_only=True)
        except RuntimeError:
            extra = []
        seen = {it["item_code"] for it in items}
        items += [it for it in extra if it["item_code"] not in seen
                  and not any(n in it["name"] for n in ng)
                  and it["review_count"] >= w.get("min_reviews", 0)]
        for it in items:
            # 実質価格＝価格−獲得ポイント（通常1倍=1%として計算）
            it["effective"] = round(it["price"] * (1 - it["point_rate"] / 100))
        items.sort(key=lambda x: x["effective"])
        # 重複を除く：同じ商品（色違い・出品違い）は一番安い1件だけ、ほかの項目に出た商品も除く
        top, keys = [], set()
        for it in items:
            k = dedupe_key(it)
            k2 = (it.get("shop", ""), k[:6])   # 同じショップの香り違い・サイズ違い
            k3 = "".join(short_name(it["name"], 80).split()[:3])   # 別ショップの同じ商品（名前の最初の3語が同じ）
            if it["item_code"] in shown or k in keys or k2 in keys or k3 in keys:
                continue
            keys.update([k, k2, k3]); shown.add(it["item_code"]); top.append(it)
            if len(top) == 5:
                break

        h = [x for x in hist.get(w["name"], []) if x["date"] >= cutoff and x["date"] != today]
        if top:
            h.append({"date": today, "min_effective": top[0]["effective"], "min_price": min(i["price"] for i in top)})
        hist[w["name"]] = h

        past = [x["min_effective"] for x in h if x["date"] != today]
        results.append({
            "name": w["name"],
            "items": top,
            "low_30": min([x["min_effective"] for x in h if x["date"] >= (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")] or [None]) if h else None,
            "is_new_low": bool(top and past and top[0]["effective"] < min(past)),
            "days": len(h),
        })

    save_json(hist_path, hist)
    return results


def articles_html():
    """site/articles/ の記事を新しい順に一覧にする。"""
    arts = sorted((SITE / "articles").glob("*.html"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not arts:
        return ""
    items = []
    for f in arts:
        m = re.search(r"<title>(.*?)</title>", f.read_text(encoding="utf-8"))
        items.append(f"<li><a href='articles/{f.name}'>{m.group(1) if m else f.stem}</a></li>")
    return f"<section><h2>比較記事</h2><ul>{''.join(items)}</ul></section>"


def picks_html(picks):
    """今日の注目：ランキングからジャンルごとに2件ずつ、画像付きカードで（毎日入れ替わる）。"""
    if not picks:
        return ""
    esc = html.escape
    by, cards = {}, []
    # ポイント倍率が高い順を優先（同じ倍率ならレビューが多い順）。ジャンルごとに最大2件
    seen = set()
    for p in sorted(picks, key=lambda p: (-p.get("point_rate", 1), -p.get("review_count", 0))):
        k = dedupe_key(p)
        if k in seen:
            continue
        if len(by.setdefault(p["genre"], [])) < 2:
            by[p["genre"]].append(p); seen.add(k)
    flat = sorted((it for items in by.values() for it in items),
                  key=lambda p: (-p.get("point_rate", 1), -p.get("review_count", 0)))
    for it in flat:
        g = it["genre"]
        if True:
            pt = ""
            badge = f"<span class=hot>🔥ポイント{it['point_rate']}倍</span>" if it.get("point_rate", 1) >= 2 else ""
            img = badge + (f"<img src='{esc(it['image'])}' alt='' loading=lazy>" if it.get("image") else "<div class=noimg>No Image</div>")
            cards.append(
                f"<a class=card href='{esc(it['url'])}' rel='sponsored nofollow noopener' target=_blank>"
                f"<span class=gtag>{esc(g)}・{it['rank']}位</span>{img}"
                f"<span class=nm>{esc(short_name(it['name'], 40))}</span>"
                f"<span class=yen>¥{it['price']:,}</span>"
                f"<span class=pts>ポイント{it.get('point_rate', 1)}倍（{it['price'] * it.get('point_rate', 1) // 100:,}pt）</span>"
                f"<span class=rv>⭐{it['review_avg']}（{it['review_count']:,}件）</span>"
                f"<span class=go>楽天で見る →</span></a>")
    return ("<section><h2>ポイント倍率が高い商品（楽天ランキングから・毎朝入れ替え）</h2>"
            "<div class=cards>" + "".join(cards) + "</div></section>")


AMZ_ICON = {"モバイルバッテリー": "🔋", "イヤホン": "🎧", "充電器": "🔌", "化粧水": "🧴", "日焼け止め": "☀️",
            "サプリ": "💊", "洗濯洗剤": "🧺", "柔軟剤": "🌸", "トイレ": "🚽", "ティッシュ": "🧻", "トイレットペーパー": "🧻",
            "ミネラルウォーター": "💧", "米": "🍚", "プロテイン": "💪", "シャンプー": "🫧", "歯ブラシ": "🪥", "加湿器": "💨", "マウス": "🖱️"}


def amazon_html(cfg, watch_names):
    """Amazonの紹介。APIが使えるまでは画像・価格は出せない（楽天の画像の流用も不可）ので、アイコンのカードで検索ページへ。"""
    tag = cfg.get("amazon_tag")
    if not tag:
        return ""
    esc = html.escape
    def icon(n):
        return next((v for k, v in AMZ_ICON.items() if k in n), "🛒")
    used = []
    try:
        arts = json.loads((DATA / "articles.json").read_text(encoding="utf-8"))
        for a in arts:
            for x in a.get("amazon", []):
                used.append(x)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    cards = []
    for x in used:
        u = f"https://www.amazon.co.jp/s?k={urllib.parse.quote(x['kw'])}&tag={tag}"
        cards.append(f"<a class='card amzc' href='{esc(u)}' rel='sponsored nofollow noopener' target=_blank>"
                     f"<span class=gtag>実際に使ったもの</span><span class=ic>{icon(x['kw'])}</span>"
                     f"<span class=nm>{esc(x['hook'])}</span><span class=rv>{esc(x['body'][:48])}…</span>"
                     f"<span class='go amz'>Amazonで見る →</span></a>")
    for n in watch_names:
        u = f"https://www.amazon.co.jp/s?k={urllib.parse.quote(n)}&tag={tag}"
        cards.append(f"<a class='card amzc' href='{esc(u)}' rel='sponsored nofollow noopener' target=_blank>"
                     f"<span class=gtag>Amazonの売れ筋から探す</span><span class=ic>{icon(n)}</span>"
                     f"<span class=nm>{esc(n)}</span><span class='go amz'>Amazonで比べる →</span></a>")
    return "<section><h2>Amazonでも比べる</h2><div class=cards>" + "".join(cards) + "</div></section>"


def kobo_section(api, cfg, today):
    """本（楽天Kobo電子書籍）：ジャンルごとに、人気上位から日替わりで4冊＋新着2冊。"""
    secs = cfg.get("kobo_sections") or []
    if not secs:
        return ""
    esc = html.escape
    n = datetime.strptime(today, "%Y-%m-%d").toordinal()
    out, shown = [], set()
    # 2日以内に読み取ったポイントだけ使う
    raw = load_json(DATA / "book_points.json", {})
    lim = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
    book_points = {k: v for k, v in raw.items() if v.get("date", "") >= lim}
    for sec in secs:
        popular, newest = [], []
        try:
            for gid in sec["genres"]:
                popular += api.kobo_books(gid, "reviewCount", 30)
                newest += [b for b in api.kobo_books(gid, "-releaseDate", 20) if b["price"] > 0][:4]
        except RuntimeError as e:
            print(f"   ⚠ Koboの取得に失敗（{sec['name']}）: {e}", file=sys.stderr)
            continue
        uniq = {}
        for b in sorted(popular, key=lambda b: -b["review_count"]):
            if b["item_number"] not in uniq and b["price"] > 0:
                uniq[b["item_number"]] = b
        popular = list(uniq.values())[:30]
        if not popular:
            continue
        # Chromeで読み取ったポイント（毎朝6:40のタスク）があれば、還元率が高い順に並べる
        for b in popular:
            pinfo = book_points.get(b["item_number"])
            b["rate"] = pinfo["rate"] if pinfo else 1
            b["pts"] = pinfo["points"] if pinfo else b["price"] // 100
        books = []
        if any(b["rate"] > 1 for b in popular) or book_points:
            ranked = sorted(popular, key=lambda b: (-b["rate"], -b["pts"], -b["review_count"]))
            for b in ranked:
                if b["item_number"] not in shown:
                    books.append(("ポイント" if b["rate"] > 1 else "人気", b)); shown.add(b["item_number"])
                if len(books) == 4:
                    break
        else:
            start = (n * 4) % len(popular)
            for i in range(len(popular)):
                b = popular[(start + i) % len(popular)]
                if b["item_number"] not in shown:
                    books.append(("人気", b)); shown.add(b["item_number"])
                if len(books) == 4:
                    break
        for b in newest:
            if b["item_number"] not in shown and len(books) < 6:
                books.append(("新着", b)); shown.add(b["item_number"])
        cards = []
        for tag, b in books:
            img = (f"<span class=hot>🔥ポイント{b['rate']}倍</span>" if b.get("rate", 1) >= 2 else "") + (f"<img class=book src='{esc(b['image'])}' alt='' loading=lazy>" if b.get("image") else "<div class=noimg>No Image</div>")
            rv = f"⭐{b['review_avg']}（{b['review_count']:,}件）" if b["review_count"] else "レビューはまだありません"
            cards.append(
                f"<a class=card href='{esc(b['url'])}' rel='sponsored nofollow noopener' target=_blank>"
                f"<span class=gtag>{tag}・{esc(b['author'][:16])}</span>{img}"
                f"<span class=nm>{esc(short_name(b['title'], 40))}</span>"
                f"<span class=yen>¥{b['price']:,}</span><span class=pts>ポイント{b.get('rate', 1)}倍（{b.get('pts', b['price'] // 100):,}pt）</span>"
                f"<span class=rv>{rv}</span><span class=go>楽天Koboで見る →</span></a>")
        out.append(f"<section><h2>{esc(sec['name'])}の本（楽天Kobo電子書籍・ポイント還元が高い順）</h2>"
                   "<div class=cards>" + "".join(cards) + "</div></section>")
    return "".join(out)


def render_site(cfg, results, stamp, picks=None, books_html=""):
    esc = html.escape
    sections = []
    for r in results:
        rows = []
        for i, it in enumerate(r["items"], 1):
            hot = " hotpt" if it["point_rate"] >= 5 else ""
            pt = (f"<span class='pt{hot}'>{'🔥' if hot else ''}ポイント{it['point_rate']}倍<br>{it['price'] * it['point_rate'] // 100:,}pt</span>")
            rows.append(
                f"<tr>"
                f"<td><a class=row href='{esc(it['url'])}' rel='sponsored nofollow noopener' target=_blank>"
                + (f"<img class=th src='{esc(it['image'])}' alt='' loading=lazy>" if it.get("image") else "")
                + f"<span>{esc(short_name(it['name'], 60))}</span></a>"
                f"<div class=shop>{esc(it['shop'])}・⭐{it['review_avg']}（{it['review_count']}件）</div></td>"
                f"<td class=num>¥{it['price']:,}{pt}</td><td class='num eff'>¥{it['effective']:,}</td></tr>"
            )
        badge = ""
        low = f"直近30日の実質最安：¥{r['low_30']:,}" if r["low_30"] else ""
        amz = ""
        if cfg.get("amazon_tag"):
            kw = urllib.parse.quote(r.get("amazon_keyword") or r["name"])
            amz = (f"<p class=amz><a href='https://www.amazon.co.jp/s?k={kw}&tag={cfg['amazon_tag']}' "
                   f"rel='sponsored nofollow noopener' target=_blank>Amazonで「{esc(r['name'])}」を見る →</a></p>")
        sections.append(
            f"<section><h2>{esc(r['name'])}の実質最安 {badge}</h2><p class=meta>{low}</p>{amz}"
            f"<div class=scroll><table><thead><tr><th></th><th>価格</th><th>実質</th></tr></thead>"
            f"<tbody>{''.join(rows) or '<tr><td colspan=3>該当なし</td></tr>'}</tbody></table></div></section>"
        )

    page = f"""<!doctype html><html lang=ja><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{esc(cfg['site_title'])}</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1917;--mut:#78716c;--line:#e7e5e4;--acc:#bf0000;--card:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1917;--fg:#f5f5f4;--mut:#a8a29e;--line:#44403c;--acc:#f87171;--card:#292524}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 Meiryo,"メイリオ","Hiragino Sans","Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif}}
main{{max-width:860px;margin:auto;padding:16px}}
.disc{{font-size:11px;color:var(--mut);margin:4px 0 12px}}
.pr{{border:1px solid var(--line);background:var(--card);padding:8px 12px;border-radius:8px;font-size:13px;color:var(--mut)}}
section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin:16px 0}}
h1{{font-size:22px}} h2{{font-size:17px;margin:4px 0}}
.scroll{{overflow-x:auto}} table{{width:100%;border-collapse:collapse}}
th,td{{border-bottom:1px solid var(--line);padding:8px 6px;text-align:left;vertical-align:top}}
.num{{text-align:right;white-space:nowrap}} .eff{{font-weight:700;color:var(--acc)}}
.n{{color:var(--mut)}} .shop,.meta{{font-size:12px;color:var(--mut);margin:0}}
.amz a{{display:inline-block;margin:6px 0 2px;padding:6px 12px;border-radius:8px;background:#ff9900;color:#111;text-decoration:none;font-weight:600;font-size:13px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}}
.card{{display:flex;flex-direction:column;gap:4px;border:1px solid var(--line);border-radius:12px;padding:10px;text-decoration:none;color:inherit;background:var(--card);box-shadow:0 2px 6px rgba(0,0,0,.06);transition:transform .15s}}
.card:hover{{transform:translateY(-2px)}}
.card img{{width:100%;aspect-ratio:1;object-fit:contain;background:#fff;border-radius:8px}}
.card img.book{{aspect-ratio:3/4}}
.noimg{{width:100%;aspect-ratio:1;display:flex;align-items:center;justify-content:center;background:#f3f3f3;color:#999;border-radius:8px;font-size:12px}}
.card{{position:relative}} .hot{{position:absolute;top:34px;left:14px;background:#e11d48;color:#fff;font-weight:800;font-size:12px;padding:3px 8px;border-radius:999px;box-shadow:0 2px 4px rgba(0,0,0,.2)}}
.pts{{font-size:12px;font-weight:700;color:#e11d48}}
.hotpt{{font-weight:800;background:#ffe4e6;border-radius:6px;padding:2px 4px}}
.gtag{{font-size:11px;color:var(--mut)}}
.ic{{font-size:64px;text-align:center;line-height:1.3;background:#fff7ed;border-radius:8px;padding:10px 0}}
.go.amz{{background:#ff9900;color:#111}} .nm{{font-size:13px;line-height:1.4}} .yen{{font-weight:800;color:var(--acc);font-size:16px}}
.rv{{font-size:11px;color:var(--mut)}} .go{{margin-top:auto;font-size:12px;font-weight:700;color:#fff;background:#bf0000;border-radius:6px;text-align:center;padding:5px}}
a.row{{display:flex;gap:10px;align-items:center;color:inherit}} img.th{{width:56px;height:56px;object-fit:contain;background:#fff;border-radius:6px;flex:none;border:1px solid var(--line)}}
@media (max-width:480px){{.cards{{grid-template-columns:1fr 1fr}}}}
.pt{{display:block;font-size:11px;color:var(--acc)}} .low{{font-size:12px;color:var(--acc);margin-left:6px}}
a{{color:inherit}}
</style></head><body><main>
<h1>{esc(cfg['site_title'])}</h1>
<p class=disc>PR｜楽天アフィリエイト・Amazonアソシエイトを利用しています。価格・ポイントは{stamp}時点のものです。</p>
{picks_html(picks)}
{articles_html()}
{''.join(sections)}
{books_html}
<p class=meta>最終更新：{stamp}</p>
<p class=disc>Amazonのアソシエイトとして、当サイトは適格販売により収入を得ています。</p>
</main></body></html>"""
    (SITE / "index.html").write_text(page, encoding="utf-8")


def main():
    global DATA
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="キー無しでダミーデータ実行")
    args = ap.parse_args()

    cfg = load_config(BASE)
    if not args.mock and "YOUR_" in json.dumps(cfg):
        sys.exit("config.json のキーが未設定です。まずは --mock で試してください。")
    for d in (DATA, OUT, SITE):
        d.mkdir(exist_ok=True)
    if args.mock:
        # モックの履歴は本番と混ぜない
        DATA = DATA / "mock"
        DATA.mkdir(exist_ok=True)

    now = datetime.now()
    today, stamp = now.strftime("%Y-%m-%d"), now.strftime("%Y/%m/%d %H:%M")
    api = Rakuten(cfg, mock=args.mock)

    posts = ranking_digest(api, cfg, today)
    posts = refresh_points(api, posts, cfg)
    texts = [compose_post(p, stamp) for p in posts]
    out = OUT / f"posts_{today}.txt"
    out.write_text(("\n\n" + "-" * 30 + "\n\n").join(texts) + "\n", encoding="utf-8")

    results = price_watch(api, cfg, today, exclude=[p["item_code"] for p in posts])
    render_site(cfg, results, stamp, posts, kobo_section(api, cfg, today))
    queue = build_queue(posts, texts, today, results)

    lines = ["■ 最安値（実質価格）"]
    for r in results:
        if r["items"]:
            flag = " 📉観測史上最安" if r["is_new_low"] else ""
            lines.append(f"・{r['name']}: ¥{r['items'][0]['effective']:,}{flag}")
    (OUT / f"summary_{today}.txt").write_text("\n".join(lines) + "\n\n", encoding="utf-8")

    print(f"① 投稿案 {len(texts)}件 → {out}")
    for q in queue:
        print(f"   {q['time']} 予約: {q['genre']} {q['text'].splitlines()[0][:40]}")
    print(f"③ 最安値ページ → {SITE / 'index.html'}")
    for r in results:
        if r["items"]:
            flag = " 📉最安更新" if r["is_new_low"] else ""
            print(f"   {r['name']}: 実質¥{r['items'][0]['effective']:,}{flag}")


if __name__ == "__main__":
    main()
