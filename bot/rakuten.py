"""楽天ウェブサービス 新API（openapi.rakuten.co.jp）の薄いクライアント。

- applicationId と accessKey の両方が必須
- Referer ヘッダーはアプリ登録時の「許可されたWebサイト」と一致させる
- --mock 時はネットに出ず、ダミーデータを返す（キー取得前の動作確認用）
"""
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
RANKING_URL = "https://openapi.rakuten.co.jp/ichibaranking/api/IchibaItem/Ranking/20220601"

# クラウド（GitHub Actions）用：キーは環境変数、それ以外は公開してよい config.public.json から読む
ENV_KEYS = {"application_id": "RAKUTEN_APP_ID", "access_key": "RAKUTEN_ACCESS_KEY", "affiliate_id": "RAKUTEN_AFFILIATE_ID"}


def load_config(base):
    """RAKUTEN_APP_ID が設定されていれば config.public.json + 環境変数、なければ従来どおり config.json。"""
    from pathlib import Path
    base = Path(base)
    if os.environ.get("RAKUTEN_APP_ID"):
        cfg = json.loads((base / "config.public.json").read_text(encoding="utf-8"))
        for k, env in ENV_KEYS.items():
            cfg[k] = os.environ.get(env, "")
        return cfg
    try:
        return json.loads((base / "config.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def big_image(it):
    """楽天の商品画像（128px）を300pxに差し替えてくっきり表示する。"""
    urls = it.get("mediumImageUrls") or []
    u = urls[0] if urls else ""
    if isinstance(u, dict):          # formatVersion=1 の形式にも対応
        u = u.get("imageUrl", "")
    return u.replace("_ex=128x128", "_ex=300x300")


class Rakuten:
    def __init__(self, cfg, mock=False):
        self.cfg = cfg
        self.mock = mock
        self._last = 0.0

    def _get(self, url, params):
        # 連打すると429になるので1秒あける
        wait = 1.1 - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        q = {
            "applicationId": self.cfg["application_id"],
            "accessKey": self.cfg["access_key"],
            "affiliateId": self.cfg["affiliate_id"],
            "format": "json",
            "formatVersion": 2,
            **params,
        }
        req = urllib.request.Request(
            url + "?" + urllib.parse.urlencode(q),
            headers={
                "Referer": self.cfg["referer"],
                "Origin": self.cfg["referer"].rstrip("/"),
                "User-Agent": "rakuten-affiliate-watch/1.0",
            },
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    self._last = time.time()
                    return json.load(r)
            except urllib.error.HTTPError as e:
                self._last = time.time()
                body = e.read().decode("utf-8", "replace")[:300]
                if e.code == 429 and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError(f"楽天API {e.code}: {body}") from None

    def ranking(self, genre_id):
        if self.mock:
            return _mock_ranking(genre_id)
        data = self._get(RANKING_URL, {"genreId": genre_id})
        return [
            {
                "rank": int(it["rank"]),
                "item_code": it["itemCode"],
                "name": it["itemName"],
                "price": int(it["itemPrice"]),
                "url": it.get("affiliateUrl") or it["itemUrl"],
                "point_rate": int(it.get("pointRate") or 1),
                "review_count": int(it.get("reviewCount") or 0),
                "review_avg": float(it.get("reviewAverage") or 0),
                "shop": it.get("shopName", ""),
                "image": big_image(it),
            }
            for it in data.get("Items", [])
        ]

    def item_detail(self, item_code):
        """ランキングAPIの pointRate は古いことがあるので、商品検索APIで最新の倍率と終了日時を取り直す。"""
        if self.mock:
            return None
        items = self._get(SEARCH_URL, {"itemCode": item_code, "hits": 1}).get("Items", [])
        if not items:
            return None
        it = items[0]
        return {
            "price": int(it["itemPrice"]),
            "point_rate": int(it.get("pointRate") or 1),
            "point_end": it.get("pointRateEndTime") or "",
            "review_count": int(it.get("reviewCount") or 0),
            "review_avg": float(it.get("reviewAverage") or 0),
            "postage_free": it.get("postageFlag") == 0,
            "affiliate_rate": float(it.get("affiliateRate") or 0),
        }

    def search_cheapest(self, keyword, min_price=None, max_price=None, hits=30, page=1, sort="+itemPrice", genre_id=None):
        if self.mock:
            return _mock_search(keyword, min_price, max_price) if page == 1 else []
        params = {"sort": sort, "hits": hits, "availability": 1, "page": page}
        if keyword:
            params["keyword"] = keyword
        if genre_id:
            params["genreId"] = genre_id
        if min_price:
            params["minPrice"] = min_price
        if max_price:
            params["maxPrice"] = max_price
        data = self._get(SEARCH_URL, params)
        return [
            {
                "item_code": it["itemCode"],
                "name": it["itemName"],
                "price": int(it["itemPrice"]),
                "url": it.get("affiliateUrl") or it["itemUrl"],
                "point_rate": int(it.get("pointRate") or 1),
                "review_count": int(it.get("reviewCount") or 0),
                "review_avg": float(it.get("reviewAverage") or 0),
                "shop": it.get("shopName", ""),
                "image": big_image(it),
            }
            for it in data.get("Items", [])
        ]


# ---- モック（キー取得前の動作確認用。日によって少しずつ変わる） ----
def _rng(seed):
    return random.Random(f"{seed}-{time.strftime('%Y%m%d')}")


def _mock_ranking(genre_id):
    r = _rng(genre_id)
    pool = [f"【モック】商品{genre_id % 1000}-{i:02d} 高評価モデル 2026年版" for i in range(60)]
    picked = r.sample(range(60), 30)
    return [
        {
            "rank": n + 1,
            "item_code": f"mockshop:{genre_id}-{i}",
            "name": pool[i],
            "price": 1980 + i * 370,
            "url": f"https://example.com/mock/{genre_id}/{i}",
            "point_rate": r.choice([1, 1, 1, 2, 5, 10]),
            "review_count": r.randint(0, 3000),
            "review_avg": round(r.uniform(3.5, 4.9), 2),
            "shop": "モックショップ",
        }
        for n, i in enumerate(picked)
    ]


def _mock_search(keyword, min_price, max_price):
    r = _rng(keyword)
    lo, hi = min_price or 1000, max_price or 20000
    items = []
    for i in range(12):
        items.append({
            "item_code": f"mockshop:{abs(hash(keyword)) % 10000}-{i}",
            "name": f"【モック】{keyword} モデル{i}",
            "price": r.randint(lo, hi),
            "url": f"https://example.com/mock/search/{i}",
            "point_rate": r.choice([1, 1, 2, 5, 10]),
            "review_count": r.randint(0, 800),
            "review_avg": round(r.uniform(3.2, 4.8), 2),
            "shop": f"モック店{i % 4}",
            "image": "",
        })
    return sorted(items, key=lambda x: x["price"])
