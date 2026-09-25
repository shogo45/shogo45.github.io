"""「PRっぽくない構成」でN日分のX投稿を作る（Chromeでまとめて予約する用）。

1日4件：07:40 暮らしのコツ／12:10 買い物のコツ（どちらもリンクなし・PRなし）
        18:30 体験ベースの記事紹介（自サイトの記事へ）／21:00 セールの目玉（楽天リンク）。どちらも最後の行に単独で【PR】
体験は data/articles.json（本人の言葉）だけを使う。倍率は予約時刻に有効なものだけ書く。
"""
import json
import urllib.parse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from rakuten import Rakuten
import run_daily
from run_daily import compose_post, ranking_digest, refresh_points, x_weight
from select_posts import score, load_history, NO_REPEAT_DAYS

BASE = Path(__file__).resolve().parent
SITE = "https://totalyamanote.github.io/"

SHOPPING_TIPS = [
    "楽天で比べるときは「価格」より「実質価格（価格−ポイント）」で見ると、どこが本当に安いか分かりやすい。",
    "レビューは★5より★1〜2を5件だけ読むのがおすすめ。自分に合わない理由が書いてあるかどうかで判断できる。",
    "日用品は、ポイントが高い日にまとめ買いするのが一番ラク。詰め替え用ならストックしても場所をとらない。",
    "同じ商品でもショップによって送料とポイント倍率が違う。カートに入れる前に1回だけ見比べると損しにくい。",
    "「レビュー件数が多い＝ハズレにくい」は日用品ではかなり当たる。迷ったら件数の多い定番から。",
]


def main(start, days):
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    arts = json.loads((BASE / "data" / "articles.json").read_text(encoding="utf-8"))
    life_tips = [t for a in arts for t in a["tips"]]
    api = Rakuten(cfg)
    now = datetime.now()
    cands = refresh_points(api, ranking_digest(api, cfg, now.strftime("%Y-%m-%d")), cfg)
    recent = {h.get("item_code") for h in load_history()
              if (now - datetime.fromisoformat(h["date"])).days < NO_REPEAT_DAYS}
    pool = sorted([c for c in cands if c["item_code"] not in recent and c["review_count"] >= 30
                   and c.get("point_end")], key=score, reverse=True)
    used, shops, batch = set(), set(), []
    stamp = now.strftime("%Y/%m/%d %H:%M")
    for i in range(days):
        day = start + timedelta(days=i)
        d = f"{day:%Y-%m-%d}"
        batch.append({"at": f"{d} 07:40", "kind": "tip", "text": life_tips[i % len(life_tips)]})
        batch.append({"at": f"{d} 12:10", "kind": "tip", "text": SHOPPING_TIPS[i % len(SHOPPING_TIPS)]})
        if i % 2 == 0:
            a = arts[(i // 2) % len(arts)]
            text = f"{a['hook']}\n\n{a['body']}\n\n比較記事にまとめました👇\n{SITE}articles/{a['file']}\n{a['tag']}\n【PR】"
        else:
            v = [("楽天の人気商品を「価格−ポイント＝実質価格」で毎朝並べ直してます。\n\n"
                  "モバイルバッテリー、化粧水、日焼け止め、サプリなど。買う前にどうぞ👇\n"),
                 ("楽天で買う前に「実質いくらか」だけ確認できるページを作りました。\n\n"
                  "ポイント込みの最安を毎朝自動で更新してます。日用品のまとめ買い前にどうぞ👇\n")][(i // 2) % 2]
            text = v + SITE + "\n【PR】"
        batch.append({"at": f"{d} 18:30", "kind": "article", "text": text})
        at = datetime.strptime(f"{d} 21:00", "%Y-%m-%d %H:%M")
        amz_items = [x for a in arts for x in a.get("amazon", [])]
        if cfg.get("amazon_tag") and amz_items and (start.toordinal() + i) % 2 == 1:
            # 1日おきにAmazon：本人が実際に買って使った商品だけ。価格は書かない（Amazonの規約）
            x = amz_items[((start.toordinal() + i) // 2) % len(amz_items)]
            url = f"https://www.amazon.co.jp/s?k={urllib.parse.quote(x['kw'])}&tag={cfg['amazon_tag']}"
            batch.append({"at": f"{d} 21:00", "kind": "amazon",
                          "text": f"{x['hook']}\n\n{x['body']}\n\nAmazonはこちら👇\n{url}\n#Amazon\n【PR】"})
            continue
        for c in pool:
            end = datetime.strptime(c["point_end"][:16], "%Y-%m-%d %H:%M")
            if c["item_code"] in used or c.get("shop") in shops or end <= at + timedelta(hours=3):
                continue
            used.add(c["item_code"]); shops.add(c.get("shop"))
            batch.append({"at": f"{d} 21:00", "kind": "deal", "item_code": c["item_code"],
                          "text": compose_post(c, stamp)})
            break
    for b in batch:
        assert x_weight(b["text"]) <= 280, (b["at"], x_weight(b["text"]))
    out = BASE / "out" / f"week_{start:%Y-%m-%d}.json"
    out.write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    return batch


if __name__ == "__main__":
    start = datetime.strptime(sys.argv[1], "%Y-%m-%d")
    for b in main(start, int(sys.argv[2])):
        print("=" * 6, b["at"], b["kind"]); print(b["text"])
