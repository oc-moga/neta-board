#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
歴史画像収集ツール（v1）

題材を入れると、ジャパンサーチ / Wikimedia Commons / メトロポリタン美術館から
高解像度の画像を探して、PCのフォルダにダウンロードする。

使い方（Windows）:
    py image_collector.py 黒船来航
    py image_collector.py 黒船来航 島原の乱        （スペースで複数の題材）
    py image_collector.py 黒船来航 --target 15      （採用枚数を変える）
    py image_collector.py 黒船来航 --no-download    （CSVだけ作る）
    py image_collector.py 坂本龍馬 --kind 人物      （肖像画・人物写真だけ。地図/風景/文書 もある）

必要なもの: Python 3.8以上、requests（任意で pillow）
    py -m pip install requests pillow
"""
import argparse
import csv
import re
import sys
import time
import urllib.parse
from functools import lru_cache
from pathlib import Path

import requests

try:
    from PIL import Image  # 任意：ダウンロード後に実寸を測る
except ImportError:
    Image = None

# Windowsのコンソールで日本語が原因で落ちないように
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ============================== 設定 ==============================
UA = "HistoryImageCollector/1.0 (personal use; python-requests)"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
SYNONYMS_CSV = BASE_DIR / "synonyms.csv"      # 表記ゆれ辞書（自分で育てる）
HITS_CSV = BASE_DIR / "keyword_hits.csv"      # どの表記が当たったかの記録

TARGET = 10               # 1題材あたりの採用枚数
PER_SOURCE_LIMIT = 8      # 各取得元から集める候補の上限
JPS_FETCH_SIZE = 100      # ジャパンサーチで絞り込み前に取る件数
MET_CHECK_LIMIT = 40      # メットで中身を確認する最大件数
MAX_DOWNLOAD_MB = 80      # これより大きい画像はスキップ
OTHER_PAGES_LIMIT = 10    # 「同じ画像が載っているページ」を1枚あたり最大何件まで記録するか

# ジャパンサーチで使ってOKとみなす権利区分（運用ルールに合わせて編集）
ALLOWED_RIGHTS = {"pdm", "cc0", "ccby", "ccbysa", "com"}

# 0件・不足のときに、上から順に条件を緩めて再検索する
LEVELS = [
    {"name": "標準",                        "min_width": 1280, "commons_pd_only": True},
    {"name": "緩和1(幅1000px以上)",          "min_width": 1000, "commons_pd_only": True},
    {"name": "緩和2(CommonsのCC BY等も含む)", "min_width": 1000, "commons_pd_only": False},
    {"name": "緩和3(幅700px以上)",           "min_width": 700,  "commons_pd_only": False},
]

# 画像の種類で絞り込む（タイトル・カテゴリ・タグなどで判定）
#   include … この言葉が入っているものだけ残す
#   exclude … include に当たっても、この言葉が入っていたら外す
# 「|」は「または」の意味。言葉を足したいときは「|新しい言葉」を書き足せばOK
KIND_FILTERS = {
    "すべて": None,
    "人物（肖像画・人物写真）": {
        "include": r"肖像|御影|人物|画像|portrait|bijin|beauties|courtesan|\bactors?\b",
        "exclude": r"銅像|石像|立像|記念碑|墓|statue|monument|sculpture|bust\b|grave|tomb",
    },
    "地図・絵図": {
        "include": r"地図|絵図|図会|国絵図|城下図|\bmaps?\b|\bplan of\b|\bcharts?\b|cartograph",
        "exclude": r"",
    },
    "風景・建物": {
        "include": (r"風景|景色|名所|眺|城|寺|神社|橋|港|湊|\bviews?\b|landscape|scenery|castle|temple"
                    r"|shrine|bridge|harbou?r|port of|building|architecture|street"),
        "exclude": r"",
    },
    "文書・書状": {
        "include": r"書状|文書|書簡|手紙|日記|覚書|証文|古文書|\bletters?\b|document|manuscript|diary",
        "exclude": r"",
    },
}
# ==================================================================


# ---------- 共通ユーティリティ ----------
@lru_cache(maxsize=None)
def get_json(url):
    """GETしてJSONを返す。同じURLは再取得しない（条件緩和の再検索を速くするため）"""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        time.sleep(0.2)  # 相手サーバーに優しく
        if r.status_code != 200:
            print(f"  [警告] HTTP {r.status_code}: {url[:90]}")
            return None
        return r.json()
    except Exception as e:  # noqa: BLE001
        print(f"  [警告] 取得失敗: {e}")
        return None


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def first(v, default=""):
    lst = as_list(v)
    return lst[0] if lst else default


def safe_name(s, maxlen=40):
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", str(s)).strip("_.")
    return s[:maxlen] or "untitled"


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def size_info(w, h):
    if not (w and h):
        return "不明", "不明", "不明"
    r = w / h
    return f"{w}x{h}", f"{r:.2f}", ("○" if 1.6 <= r <= 2.0 else "×(パン・ズーム向き)")


def matches_kind(text, kind_filter):
    """種類の絞り込み。kind_filter が None（=すべて）なら常にOK"""
    if not kind_filter:
        return True
    text = text or ""
    if not re.search(kind_filter["include"], text, re.I):
        return False
    if kind_filter.get("exclude") and re.search(kind_filter["exclude"], text, re.I):
        return False
    return True


def make_candidate(source, title, image_url, page_url, provider, database,
                   rights, credit, kind, width, height, period):
    return {
        "source": source, "title": title or "", "image_url": image_url,
        "page_url": page_url or "", "provider": provider or "", "database": database or "",
        "rights": rights or "不明", "credit": credit or "", "kind": kind,
        "width": width, "height": height, "period": period or "",
        "keyword": "", "level": "", "file": "",
        # 同じ画像が載っている別のページ [{"label": 表示名, "url": URL}, ...]
        "other_pages": [], "other_pages_total": 0,
    }


def add_page(cand, label, url):
    """other_pages に追加（同じURL・出典ページと同じURLは入れない）"""
    if not url or url == cand["page_url"]:
        return
    if any(p["url"] == url for p in cand["other_pages"]):
        return
    cand["other_pages"].append({"label": label, "url": url})


def is_top_page(url):
    """https://example.com/ のようなトップページだけのURLか（画像のページではないので除く）"""
    return urllib.parse.urlparse(url).path in ("", "/")


def reverse_search_links(image_url):
    """Web全体から同じ画像を探すための検索リンク（クリックすると検索結果が開く）"""
    q = urllib.parse.quote(image_url, safe="")
    return [
        {"label": "Googleレンズで探す", "url": f"https://lens.google.com/uploadbyurl?url={q}"},
        {"label": "TinEyeで探す", "url": f"https://tineye.com/search?url={q}"},
    ]


def other_pages_text(cand):
    """CSV用：URLを「 ; 」でつないだ文字列"""
    return " ; ".join(p["url"] for p in cand.get("other_pages", []))


# ---------- 取得元① ジャパンサーチ ----------
def iiif_info(manifest_url):
    """IIIFマニフェスト(v2/v3)から、1枚目の高解像度URLと元寸法を返す"""
    m = get_json(manifest_url)
    if not m:
        return None
    try:
        if "sequences" in m:       # IIIF Presentation v2
            canvas = m["sequences"][0]["canvases"][0]
            service = canvas["images"][0]["resource"].get("service")
            size = "full"
        elif "items" in m:         # IIIF Presentation v3
            canvas = m["items"][0]
            body = canvas["items"][0]["items"][0]["body"]
            if isinstance(body, list):
                body = body[0]
            service = body.get("service")
            size = "max"
        else:
            return None
        if isinstance(service, list):
            service = service[0] if service else None
        if not service:
            return None
        base = service.get("@id") or service.get("id")
        if not base:
            return None
        return {
            "url": f"{base.rstrip('/')}/full/{size}/0/default.jpg",
            "width": canvas.get("width"),
            "height": canvas.get("height"),
        }
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def fetch_jps(keyword, level, limit, kind_pattern=None):
    url = "https://jpsearch.go.jp/api/item/search/jps-cross?" + urllib.parse.urlencode(
        {"keyword": keyword, "size": JPS_FETCH_SIZE})
    data = get_json(url) or {}
    out = []
    for item in data.get("list", []):
        c = item.get("common") or {}
        rights = c.get("contentsRightsType") or ""
        if rights not in ALLOWED_RIGHTS:
            continue
        # 種類の判定はタイトル（日本語・英語）だけで行う
        # （説明文まで見ると「画像」などの言葉が別の意味で引っかかりやすいため）
        kind_text = f"{first(c.get('title'))} {c.get('titleEn') or ''}"
        if not matches_kind(kind_text, kind_pattern):
            continue

        image_url = kind = None
        width = height = None

        manifest = first(c.get("iiifUrl"), None)
        if manifest:
            info = iiif_info(manifest)
            if info:
                image_url, width, height = info["url"], info["width"], info["height"]
                kind = "高解像度(IIIF)"
        if not image_url:
            cu = first(c.get("contentsUrl"), "")
            if cu and re.search(r"\.(jpe?g|png)(\?|$)", str(cu), re.I):
                image_url, kind = cu, "画像URL(解像度要確認)"
        if not image_url:
            continue  # サムネ止まりは採用しない
        if width and width < level["min_width"]:
            continue

        jps_page = f"https://jpsearch.go.jp/item/{item.get('id', '')}"
        cand = make_candidate(
            "jps", str(first(c.get("title"))), image_url,
            first(c.get("linkUrl")) or jps_page,
            str(first(c.get("provider"))), str(first(c.get("database"))),
            rights, str(first(c.get("provider"))), kind, width, height,
            " / ".join(str(t) for t in as_list(c.get("temporal"))))
        # 同じ資料の別ページ：ジャパンサーチ自身のページ／所蔵館のページ
        add_page(cand, "ジャパンサーチ", jps_page)
        provider_url = str(first(c.get("providerUrl"), "") or "")
        if provider_url and not is_top_page(provider_url):
            add_page(cand, f"所蔵館のページ（{cand['provider']}）", provider_url)
        out.append(cand)
        if len(out) >= limit:
            break
    return out


# ---------- 取得元② Wikimedia Commons ----------
def fetch_commons(query, level, limit, kind_pattern=None):
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrnamespace": 6, "gsrlimit": 50, "gsrsearch": query + " filetype:bitmap",
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
    }
    data = get_json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)) or {}
    pages = list(((data.get("query") or {}).get("pages") or {}).values())
    pages.sort(key=lambda p: p.get("index", 0))

    out = []
    for p in pages:
        ii = (p.get("imageinfo") or [None])[0]
        if not ii or ii.get("mime") not in ("image/jpeg", "image/png"):
            continue
        w, h = ii.get("width") or 0, ii.get("height") or 0
        if w < level["min_width"]:
            continue
        meta = ii.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        if level["commons_pd_only"] and not re.search(r"public domain|^pd|cc0", lic, re.I):
            continue
        # 種類の判定：ファイル名・作品名・カテゴリ（説明文はHTMLを除いて先頭だけ）
        kind_text = " ".join([
            p.get("title", ""),
            strip_tags((meta.get("ObjectName") or {}).get("value", "")),
            (meta.get("Categories") or {}).get("value", "").replace("|", " "),
            strip_tags((meta.get("ImageDescription") or {}).get("value", ""))[:300],
        ])
        if not matches_kind(kind_text, kind_pattern):
            continue
        artist = strip_tags((meta.get("Artist") or {}).get("value", ""))

        out.append(make_candidate(
            "commons", re.sub(r"^File:", "", p.get("title", "")), ii.get("url"),
            ii.get("descriptionurl"), "Wikimedia Commons", "commons",
            lic or "不明", artist, "高解像度(原画像)", w, h, ""))
        if len(out) >= limit:
            break
    return out


# ---------- 取得元③ メトロポリタン美術館 ----------
def fetch_met(query, level, limit, kind_pattern=None):
    base = "https://collectionapi.metmuseum.org/public/collection/v1/"
    # 検索は2026-10-01に v1 が廃止されて v1.1 に移った（offset と limit で必要な分だけ取る形）
    # 作品の中身を取る objects は今も v1 のまま
    search_url = "https://collectionapi.metmuseum.org/public/collection/v1.1/search?" + urllib.parse.urlencode(
        {"hasImages": "true", "q": query, "offset": 0, "limit": MET_CHECK_LIMIT})
    data = get_json(search_url) or {}
    ids = (data.get("objectIDs") or [])[:MET_CHECK_LIMIT]
    out = []
    for oid in ids:
        o = get_json(f"{base}objects/{oid}")
        if not o or not o.get("isPublicDomain") or not o.get("primaryImage"):
            continue
        # 種類の判定：タイトル・作品の種類・分類・タグ（例：Portraits, Men, Maps）
        tags = " ".join(t.get("term", "") for t in (o.get("tags") or []) if isinstance(t, dict))
        kind_text = " ".join(str(o.get(k) or "") for k in ("title", "objectName", "classification")) + " " + tags
        if not matches_kind(kind_text, kind_pattern):
            continue
        cand = make_candidate(
            "met", o.get("title"), o["primaryImage"], o.get("objectURL"),
            "The Metropolitan Museum of Art", "met_open_access",
            "cc0(Met Open Access)", "The Metropolitan Museum of Art",
            "高解像度(原画像・寸法はDL後に判明)", None, None, o.get("objectDate"))
        add_page(cand, "Wikidata（作品のデータ）", o.get("objectWikidata_URL"))
        add_page(cand, "関連資料", o.get("linkResource"))
        out.append(cand)
        if len(out) >= limit:
            break
    return out


# ---------- 辞書（表記ゆれ） ----------
def split_terms(s):
    return [t.strip() for t in re.split(r"[;；,、]", s or "") if t.strip()]


def load_synonyms():
    syn = {}
    if SYNONYMS_CSV.exists():
        with open(SYNONYMS_CSV, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if not row or not row[0].strip():
                    continue
                row = row + [""] * (3 - len(row))
                syn[row[0].strip()] = {"jp": split_terms(row[1]), "en": split_terms(row[2])}
    return syn


def save_synonyms(syn):
    with open(SYNONYMS_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["題材", "日本語別表記", "英語キーワード"])
        for topic, v in syn.items():
            w.writerow([topic, ";".join(v["jp"]), ";".join(v["en"])])


def grow_dictionary(topic, syn):
    """実行後に、別表記をその場で辞書へ足せる（Enterでスキップ）"""
    entry = syn.setdefault(topic, {"jp": [], "en": []})
    if sys.stdin.isatty():
        jp_in = input("  辞書に足す日本語の別表記（;区切り / Enterでスキップ）> ")
        en_in = input("  辞書に足す英語キーワード（;区切り / Enterでスキップ）> ")
        for t in split_terms(jp_in):
            if t not in entry["jp"] and t != topic:
                entry["jp"].append(t)
        for t in split_terms(en_in):
            if t not in entry["en"]:
                entry["en"].append(t)
    save_synonyms(syn)


def update_hits(topic, chosen):
    """どの表記で何枚採用されたかを累積で記録（辞書を育てる手がかり）"""
    hits = {}
    if HITS_CSV.exists():
        with open(HITS_CSV, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 3 and row[2].isdigit():
                    hits[(row[0], row[1])] = int(row[2])
    for c in chosen:
        key = (topic, c["keyword"])
        hits[key] = hits.get(key, 0) + 1
    with open(HITS_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["題材", "キーワード", "累計採用枚数"])
        for (t, k), n in sorted(hits.items(), key=lambda x: (x[0][0], -x[1])):
            w.writerow([t, k, n])


# ---------- 検索の本体 ----------
def collect(topic, syn, target, kind="すべて"):
    """kind は KIND_FILTERS のキー（例："人物（肖像画・人物写真）"）。条件緩和しても種類の絞り込みは外さない"""
    kind_pattern = KIND_FILTERS.get(kind)
    entry = syn.get(topic, {"jp": [], "en": []})
    jp = [topic] + [k for k in entry["jp"] if k != topic]
    en = entry["en"]

    sources = {
        "jps": (fetch_jps, jp),
        "commons": (fetch_commons, jp + en),
        "met": (fetch_met, en),  # メットは英語キーワードがあるときだけ
    }
    chosen, seen = [], set()

    for level in LEVELS:
        pools = {}
        for src, (fn, keywords) in sources.items():
            pool, pool_urls = [], set()
            for kw in keywords:
                if len(pool) >= PER_SOURCE_LIMIT:
                    break
                for cand in fn(kw, level, PER_SOURCE_LIMIT - len(pool), kind_pattern):
                    if cand["image_url"] in seen or cand["image_url"] in pool_urls:
                        continue
                    cand["keyword"], cand["level"] = kw, level["name"]
                    pool.append(cand)
                    pool_urls.add(cand["image_url"])
            pools[src] = pool

        # 取得元ごとに1件ずつ順番に拾って混ぜる
        i = 0
        while len(chosen) < target and any(i < len(p) for p in pools.values()):
            for p in pools.values():
                if i < len(p) and len(chosen) < target and p[i]["image_url"] not in seen:
                    chosen.append(p[i])
                    seen.add(p[i]["image_url"])
            i += 1

        print(f"  [{level['name']}] 現在 {len(chosen)} 件")
        if len(chosen) >= target:
            break

    add_commons_usage(chosen)
    return chosen


def add_commons_usage(cands):
    """Commonsの画像が、どのWikipediaのページで使われているかを調べて other_pages に足す
    （採用した画像だけ調べる。日本語版→英語版→その他の順に並べる）"""
    for c in cands:
        if c["source"] != "commons":
            continue
        params = {
            "action": "query", "format": "json", "titles": "File:" + c["title"],
            "prop": "globalusage", "guprop": "url", "gunamespace": 0, "gulimit": 100,
        }
        data = get_json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)) or {}
        pages = list(((data.get("query") or {}).get("pages") or {}).values())
        usage = (pages[0].get("globalusage") or []) if pages else []
        order = {"ja.wikipedia.org": 0, "en.wikipedia.org": 1}
        usage.sort(key=lambda u: order.get(u.get("wiki"), 2))
        c["other_pages_total"] = len(usage) + len(c["other_pages"])
        for u in usage:
            if len(c["other_pages"]) >= OTHER_PAGES_LIMIT:
                break
            wiki = (u.get("wiki") or "").replace(".org", "")
            add_page(c, f"{wiki}：{u.get('title', '').replace('_', ' ')}", u.get("url"))
    for c in cands:
        c["other_pages_total"] = max(c["other_pages_total"], len(c["other_pages"]))


# ---------- ダウンロード ----------
EXT_MAP = {"image/jpeg": ".jpg", "image/png": ".png", "image/tiff": ".tif", "image/webp": ".webp"}


def download(cand, folder, index):
    try:
        r = requests.get(cand["image_url"], headers={"User-Agent": UA}, timeout=60, stream=True)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            return None, f"画像ではない({ctype or '不明'})"
        ext = EXT_MAP.get(ctype, ".jpg")
        path = folder / f"{index:02d}_{cand['source']}_{safe_name(cand['title'])}{ext}"
        limit = MAX_DOWNLOAD_MB * 1024 * 1024
        size = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                size += len(chunk)
                if size > limit:
                    f.close()
                    path.unlink(missing_ok=True)
                    return None, f"{MAX_DOWNLOAD_MB}MB超"
                f.write(chunk)
        time.sleep(0.3)
        return path, ""
    except Exception as e:  # noqa: BLE001
        return None, f"失敗: {e}"


def run_topic(topic, syn, target, do_download, kind="すべて"):
    print(f"\n=== {topic} ===" + ("" if kind == "すべて" else f"（種類：{kind}）"))
    chosen = collect(topic, syn, target, kind)
    folder = OUTPUT_DIR / safe_name(topic)
    folder.mkdir(parents=True, exist_ok=True)

    rows = []
    for i, c in enumerate(chosen, 1):
        status = ""
        if do_download:
            path, err = download(c, folder, i)
            if path:
                c["file"] = path.name
                if Image and not (c["width"] and c["height"]):
                    try:
                        with Image.open(path) as im:
                            c["width"], c["height"] = im.size
                    except Exception:  # noqa: BLE001
                        pass
                print(f"  {i:02d} OK  {path.name}")
            else:
                status = f"DL失敗({err})"
                print(f"  {i:02d} NG  {c['title'][:30]} : {err}")
        sz, ratio, fit = size_info(c["width"], c["height"])
        rows.append([
            topic, c["keyword"], c["title"], c["file"] or status, c["image_url"], c["page_url"],
            c["provider"], c["database"], c["rights"], c["credit"], c["kind"],
            sz, ratio, fit, c["period"], c["level"], other_pages_text(c),
        ])

    with open(folder / "results.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["題材", "ヒットしたキーワード", "タイトル", "保存ファイル", "画像URL", "掲載元ページURL",
                    "所蔵機関", "データベース", "権利区分", "クレジット", "画像区分",
                    "元画像サイズ", "縦横比", "16:9向き", "時代", "緩和レベル", "同じ画像が載っているページ"])
        w.writerows(rows)

    update_hits(topic, chosen)
    print(f"  -> {len(chosen)} 件 / 保存先: {folder}")
    if len(chosen) < target:
        print("  足りなかったので、辞書に別表記や英語キーワードを足してもう一度回すと増えやすい")
    grow_dictionary(topic, syn)


def main():
    ap = argparse.ArgumentParser(description="歴史画像収集ツール")
    ap.add_argument("topics", nargs="*", help="題材（スペース区切りで複数OK）")
    ap.add_argument("--target", type=int, default=TARGET, help="1題材あたりの採用枚数")
    ap.add_argument("--no-download", action="store_true", help="ダウンロードせずCSVだけ作る")
    ap.add_argument("--kind", default="すべて",
                    help="画像の種類で絞り込む。人物 / 地図 / 風景 / 文書 のように先頭の言葉だけでOK")
    args = ap.parse_args()

    # 「人物」だけ書けば「人物（肖像画・人物写真）」と見なす
    kind = next((k for k in KIND_FILTERS if k.startswith(args.kind)), None)
    if kind is None:
        print(f"--kind は次のどれかにしてね：{' / '.join(KIND_FILTERS)}")
        return

    topics = args.topics or input("題材を入力（複数ならスペース区切り）> ").split()
    if not topics:
        print("題材が入力されなかったので終了")
        return

    OUTPUT_DIR.mkdir(exist_ok=True)
    syn = load_synonyms()
    for t in topics:
        run_topic(t, syn, args.target, not args.no_download, kind)
    print("\n完了")


if __name__ == "__main__":
    main()
