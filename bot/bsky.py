"""Bluesky（AT Protocol）へ投稿する。公式APIで無料。

- ログイン：config.json の bluesky.handle と bluesky.app_password（アプリパスワード。本パスワードは使わない）
- URL とハッシュタグは facets（バイト位置）を付けないとリンクにならない
- 本文は300文字（書記素）まで
"""
import json
import re
import urllib.request
from datetime import datetime, timezone

PDS = "https://bsky.social/xrpc"
URL_RE = re.compile(r"https?://[^\s　]+")
TAG_RE = re.compile(r"(?:^|(?<=[\s　]))[#＃]([^\s　#＃]+)")


def _call(method, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{PDS}/{method}", data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def login(handle, app_password):
    s = _call("com.atproto.server.createSession", {"identifier": handle, "password": app_password})
    return s["accessJwt"], s["did"]


def shorten_links(text):
    """長いアフィリエイトURLを短い表示に置き換える。リンク先（元のURL）は facets に残す。"""
    links, out, pos = [], "", 0
    for m in URL_RE.finditer(text):
        host = m.group().split("/")[2]
        label = "楽天で見る" if "rakuten" in host else "Amazonで見る" if "amazon" in host else host + "/…"
        out += text[pos:m.start()]
        links.append((len(out.encode()), len((out + label).encode()), m.group()))
        out += label
        pos = m.end()
    return out + text[pos:], links


def _facets(text, links):
    facets = [{"index": {"byteStart": s, "byteEnd": e}, "features": [{"$type": "app.bsky.richtext.facet#link", "uri": u}]}
              for s, e, u in links]
    for m in TAG_RE.finditer(text):
        s = m.start(1) - 1   # 「#」の位置から
        facets.append({"index": {"byteStart": len(text[:s].encode()), "byteEnd": len(text[:m.end()].encode())},
                       "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1)}]})
    return facets


def fit_raw(text, limit=300):
    """URLを短縮したあとの長さが300以内になるよう削る。"""
    shortened = len(shorten_links(text)[0])
    return fit(text, limit + (len(text) - shortened))


def fit(text, limit=300):
    """300文字を超えるときは、ハッシュタグの行→URL以外の最後の行の順に削る（URLは必ず残す）。"""
    if len(text) <= limit:
        return text
    lines = [l for l in text.split("\n") if not TAG_RE.match(l.strip())]
    while len("\n".join(lines)) > limit:
        over = len("\n".join(lines)) - limit
        i = max(i for i, l in enumerate(lines) if l.strip() and not URL_RE.search(l))
        lines[i] = lines[i][:-(over + 1)] + "…" if len(lines[i]) > over + 1 else ""
        lines = [l for j, l in enumerate(lines) if l or j != i]
    return "\n".join(lines)


def post(text, handle, app_password):
    token, did = login(handle, app_password)
    text, links = shorten_links(fit_raw(text))
    record = {"$type": "app.bsky.feed.post", "text": text, "langs": ["ja"],
              "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    f = _facets(text, links)
    if f:
        record["facets"] = f
    r = _call("com.atproto.repo.createRecord", {"repo": did, "collection": "app.bsky.feed.post", "record": record}, token)
    rkey = r["uri"].rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"
