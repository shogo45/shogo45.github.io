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


def price_watch(api, cfg, today):
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
        for it in items:
            # 実質価格＝価格−獲得ポイント（通常1倍=1%として計算）
            it["effective"] = round(it["price"] * (1 - it["point_rate"] / 100))
        items.sort(key=lambda x: x["effective"])
        top = items[:5]

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
    """今日の注目：ランキングからジャンルごとに2件ずつ（毎日入れ替わる）。"""
    if not picks:
        return ""
    esc = html.escape
    by, rows = {}, []
    for p in sorted(picks, key=lambda p: (-p.get("review_count", 0))):
        if len(by.setdefault(p["genre"], [])) < 2:
            by[p["genre"]].append(p)
    for g, items in by.items():
        for it in items:
            pt = f"<span class=pt>P{it['point_rate']}倍</span>" if it.get("point_rate", 1) > 1 else ""
            rows.append(f"<tr><td class=n>{esc(g)}</td><td><a href='{esc(it['url'])}' rel='sponsored nofollow noopener' target=_blank>"
                        f"{esc(short_name(it['name'], 50))}</a><div class=shop>{it['rank']}位・⭐{it['review_avg']}（{it['review_count']:,}件）</div></td>"
                        f"<td class=num>¥{it['price']:,}{pt}</td></tr>")
    return ("<section><h2>今日の注目（楽天ランキングから・毎朝入れ替え）</h2><div class=scroll><table>"
            "<thead><tr><th>ジャンル</th><th>商品</th><th>価格</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></section>")


def render_site(cfg, results, stamp, picks=None):
    esc = html.escape
    sections = []
    for r in results:
        rows = []
        for i, it in enumerate(r["items"], 1):
            pt = f"<span class=pt>P{it['point_rate']}倍</span>" if it["point_rate"] > 1 else ""
            rows.append(
                f"<tr><td class=n>{i}</td>"
                f"<td><a href='{esc(it['url'])}' rel='sponsored nofollow noopener' target=_blank>{esc(short_name(it['name'], 60))}</a>"
                f"<div class=shop>{esc(it['shop'])}・⭐{it['review_avg']}（{it['review_count']}件）</div></td>"
                f"<td class=num>¥{it['price']:,}{pt}</td><td class='num eff'>¥{it['effective']:,}</td></tr>"
            )
        badge = "<span class=low>📉 観測史上最安</span>" if r["is_new_low"] else ""
        low = f"直近30日の実質最安：¥{r['low_30']:,}（観測{r['days']}日）" if r["low_30"] else ""
        amz = ""
        if cfg.get("amazon_tag"):
            kw = urllib.parse.quote(r.get("amazon_keyword") or r["name"])
            amz = (f"<p class=amz><a href='https://www.amazon.co.jp/s?k={kw}&tag={cfg['amazon_tag']}' "
                   f"rel='sponsored nofollow noopener' target=_blank>Amazonで「{esc(r['name'])}」を見る →</a></p>")
        sections.append(
            f"<section><h2>{esc(r['name'])} {badge}</h2><p class=meta>{low}</p>{amz}"
            f"<div class=scroll><table><thead><tr><th>#</th><th>商品</th><th>価格</th><th>実質</th></tr></thead>"
            f"<tbody>{''.join(rows) or '<tr><td colspan=4>該当なし</td></tr>'}</tbody></table></div></section>"
        )

    page = f"""<!doctype html><html lang=ja><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{esc(cfg['site_title'])}</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1917;--mut:#78716c;--line:#e7e5e4;--acc:#bf0000;--card:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1917;--fg:#f5f5f4;--mut:#a8a29e;--line:#44403c;--acc:#f87171;--card:#292524}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,"Hiragino Sans",sans-serif}}
main{{max-width:860px;margin:auto;padding:16px}}
.pr{{border:1px solid var(--line);background:var(--card);padding:8px 12px;border-radius:8px;font-size:13px;color:var(--mut)}}
section{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin:16px 0}}
h1{{font-size:22px}} h2{{font-size:17px;margin:4px 0}}
.scroll{{overflow-x:auto}} table{{width:100%;border-collapse:collapse}}
th,td{{border-bottom:1px solid var(--line);padding:8px 6px;text-align:left;vertical-align:top}}
.num{{text-align:right;white-space:nowrap}} .eff{{font-weight:700;color:var(--acc)}}
.n{{color:var(--mut)}} .shop,.meta{{font-size:12px;color:var(--mut);margin:0}}
.amz a{{display:inline-block;margin:6px 0 2px;padding:6px 12px;border-radius:8px;background:#ff9900;color:#111;text-decoration:none;font-weight:600;font-size:13px}}
.pt{{display:block;font-size:11px;color:var(--acc)}} .low{{font-size:12px;color:var(--acc);margin-left:6px}}
a{{color:inherit}}
</style></head><body><main>
<h1>{esc(cfg['site_title'])}</h1>
<p class=pr>【PR】当ページは楽天アフィリエイトとAmazonアソシエイトを利用しています。Amazonのアソシエイトとして、当サイトは適格販売により収入を得ています。価格・ポイント倍率は{stamp}時点のもので、変わっている場合があります。購入前に必ず商品ページでご確認ください。<br>実質価格＝価格−獲得ポイント（通常1倍を1%として概算）。</p>
{picks_html(picks)}
{articles_html()}
{''.join(sections)}
<p class=meta>最終更新：{stamp}</p>
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

    results = price_watch(api, cfg, today)
    render_site(cfg, results, stamp, posts)
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
