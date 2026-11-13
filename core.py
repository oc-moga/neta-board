"""ネタ収集のコアロジック（画面に依存しない部分）"""
import calendar
import csv
import gzip
import io
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser

JST = ZoneInfo("Asia/Tokyo")
USER_AGENT = "neta-board/1.0 (personal use; SNS content research)"

# ===== RSS設定 =====
# base    : 媒体の拠点（裏取りで、拠点の違う媒体を見比べるための目印）
# kind    : 運営形態（出典表記に添える。立場の評価ではなく、資本・運営の事実ベース）
# mideast : True なら記事すべてを「中東関連」とする（中東専門フィード）
#           False ならキーワードに一致した記事だけを「中東関連」とする
# party_state : True は紛争当事国の政府系メディア。初期状態では表示しない
FEEDS = [
    # --- 日本 ---
    {"name": "NHK 国際", "base": "日本", "kind": "公共放送", "mideast": False,
     "url": "https://www.nhk.or.jp/rss/news/cat6.xml"},
    # --- 英国 ---
    {"name": "BBC World", "base": "英国", "kind": "公共放送", "mideast": False,
     "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "BBC Middle East", "base": "英国", "kind": "公共放送", "mideast": True,
     "url": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"},
    {"name": "Guardian World", "base": "英国", "kind": "民間", "mideast": False,
     "url": "https://www.theguardian.com/world/rss"},
    {"name": "Guardian Middle East", "base": "英国", "kind": "民間", "mideast": True,
     "url": "https://www.theguardian.com/world/middleeast/rss"},
    # --- 欧州（英国以外） ---
    {"name": "France 24 Middle East", "base": "フランス", "kind": "政府出資", "mideast": True,
     "url": "https://www.france24.com/en/middle-east/rss"},
    # --- 米国 ---
    {"name": "Fox News World", "base": "米国", "kind": "民間", "mideast": False,
     "url": "https://moxie.foxnews.com/google-publisher/world.xml"},
    {"name": "Newsmax", "base": "米国", "kind": "民間", "mideast": False,
     "url": "https://www.newsmax.com/rss/Newsfront/16/"},
    {"name": "CBN News World", "base": "米国", "kind": "宗教系(キリスト教)", "mideast": False,
     "url": "https://www1.cbn.com/app_feeds/rss/news/rss.php?section=world"},
    {"name": "CBN News Israel", "base": "米国", "kind": "宗教系(キリスト教)", "mideast": True,
     "url": "https://www1.cbn.com/app_feeds/rss/news/rss.php?section=israel"},
    {"name": "NPR World", "base": "米国", "kind": "非営利", "mideast": False,
     "url": "https://feeds.npr.org/1004/rss.xml"},
    # --- 中東 ---
    {"name": "Al Jazeera", "base": "カタール", "kind": "政府出資", "mideast": False,
     "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "The National", "base": "UAE", "kind": "王族系資本", "mideast": True,
     "url": "https://www.thenationalnews.com/arc/outboundfeeds/rss/category/news/mena/?outputType=xml"},
    {"name": "Times of Israel", "base": "イスラエル", "kind": "民間", "mideast": True,
     "url": "https://www.timesofisrael.com/feed/"},
    {"name": "Haaretz", "base": "イスラエル", "kind": "民間", "mideast": True,
     "url": "https://www.haaretz.com/srv/haaretz-latest-headlines"},
    {"name": "Tehran Times", "base": "イラン", "kind": "国営系", "mideast": True,
     "party_state": True, "url": "https://www.tehrantimes.com/rss"},
    {"name": "Anadolu Agency", "base": "トルコ", "kind": "国営通信", "mideast": True,
     "party_state": True, "url": "https://www.aa.com.tr/en/rss/default?cat=middle-east"},
    # --- 国際機関 ---
    {"name": "UN News Middle East", "base": "国連", "kind": "国際機関", "mideast": True,
     "url": "https://news.un.org/feed/subscribe/en/news/region/middle-east/feed/rss.xml"},
]
FETCH_TIMEOUT = 15  # 秒。応答しない媒体で画面が止まらないように
MAX_PER_FEED = 15   # 1フィードあたりの最大件数（新しい順）。記事数の多い媒体に一覧が偏らないように

KEYWORDS_JA = [
    "中東", "イラン", "イスラエル", "ガザ", "パレスチナ", "ハマス", "ヒズボラ",
    "レバノン", "シリア", "イエメン", "フーシ", "ホルムズ", "サウジ", "イラク",
    "カタール", "アラブ首長国", "オマーン", "ヨルダン川", "湾岸", "テヘラン",
    "バーレーン", "クウェート",
]
KEYWORDS_EN = [
    "Middle East", "Iran", "Iranian", "Israel", "Israeli", "Gaza", "Palestin\\w*",
    "Hamas", "Hezbollah", "Lebanon", "Syria", "Yemen", "Houthi\\w*", "Hormuz",
    "Saudi", "Iraq", "Qatar", "Emirates", "Oman", "West Bank", "Tehran", "Gulf",
    "Bahrain", "Kuwait", "UAE",
]
PATTERN = re.compile(
    "|".join(KEYWORDS_JA) + "|" + "|".join(rf"\b{k}\b" for k in KEYWORDS_EN),
    re.IGNORECASE,
)

NEWS_COLS = ["媒体", "拠点", "運営形態", "当事国政府系", "タイトル", "要約", "公開日時", "中東関連", "ヒットしたキーワード", "URL"]
HISTORY_COLS = ["日付", "区分", "年", "内容", "日本関連(推定)", "要確認メモ", "出典URL"]


def clean(text: str, limit: int = 200) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def find_hits(text: str) -> list:
    return sorted({m.group(0) for m in PATTERN.finditer(text)})


def _fetch_one(f: dict):
    """1媒体ぶんを取得してパース。失敗したら None"""
    try:
        req = urllib.request.Request(f["url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as res:
            data = res.read()
        if data[:2] == b"\x1f\x8b":  # 頼んでいなくてもgzipで返す媒体がある（UN Newsなど）
            data = gzip.decompress(data)
        parsed = feedparser.parse(data)
    except Exception:
        return None
    if parsed.bozo and not parsed.entries:
        return None
    return parsed


def fetch_feeds(feeds: list, hours: int):
    """RSSを取得して (記事リスト, 取得失敗した媒体名のリスト) を返す
    各記事の "mideast" は中東関連かどうか（専門フィード由来 or キーワード一致）"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items, errors, seen = [], [], {}

    # 媒体数が多いので並列で取得（結果は FEEDS の並び順のまま処理する）
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_fetch_one, feeds))

    for f, parsed in zip(feeds, results):
        if parsed is None:
            errors.append(f["name"])
            continue
        count = 0
        # 上限で切る前に新しい順へ並べ替え（フィードによっては並びが日付順でない）
        entries = sorted(parsed.entries, reverse=True,
                         key=lambda e: tuple(e.get("published_parsed")
                                             or e.get("updated_parsed") or ()))
        for e in entries:
            if count >= MAX_PER_FEED:
                break
            link = e.get("link", "")
            if not link:
                continue
            if link in seen:
                # 同じ記事が総合フィードと中東フィードの両方にある場合は、中東扱いを引き継ぐ
                seen[link]["mideast"] = seen[link]["mideast"] or f["mideast"]
                continue
            title = clean(e.get("title", ""), 150)
            summary = clean(e.get("summary", ""))
            hits = find_hits(f"{title} {summary}")

            pub = e.get("published_parsed") or e.get("updated_parsed")
            pub_dt = None
            if pub:
                # feedparserの時刻はUTC。timegmで正しくUTCとして扱う
                pub_dt = datetime.fromtimestamp(calendar.timegm(pub), tz=timezone.utc)
                if pub_dt < cutoff:
                    continue
            item = {
                "media": f["name"], "base": f["base"], "kind": f["kind"],
                "title": title, "summary": summary, "pub_dt": pub_dt,
                "hits": hits, "url": link,
                "mideast": f["mideast"] or bool(hits),
                "party_state": f.get("party_state", False),
            }
            seen[link] = item
            items.append(item)
            count += 1

    items.sort(key=lambda i: i["pub_dt"] or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    return items, errors


def pub_label(item) -> str:
    if not item["pub_dt"]:
        return "日時不明"
    return item["pub_dt"].astimezone(JST).strftime("%m/%d %H:%M")


def source_label(item) -> str:
    """例: Al Jazeera（カタール・政府出資）"""
    return f"{item['media']}（{item['base']}・{item['kind']}）"


def news_copy_text(item) -> str:
    pub = "公開日時: 不明" if not item["pub_dt"] else f"公開: {pub_label(item)}（日本時間）"
    note = "\n※紛争当事国の政府系メディアの報道" if item["party_state"] else ""
    return (f"{item['title']}\n"
            f"出典: {source_label(item)}・{pub}{note}\n"
            f"{item['url']}")


def news_rows(items) -> list:
    return [{
        "媒体": i["media"], "拠点": i["base"], "運営形態": i["kind"],
        "当事国政府系": "○" if i["party_state"] else "", "タイトル": i["title"],
        "要約": i["summary"], "公開日時": pub_label(i),
        "中東関連": "○" if i["mideast"] else "",
        "ヒットしたキーワード": " / ".join(i["hits"]), "URL": i["url"],
    } for i in items]


def to_csv_bytes(rows: list, columns: list) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


# ===== 今日は何の日 =====
SECTION_MAP = {"できごと": "できごと", "誕生日": "誕生", "忌日": "死去"}
JAPAN_WORDS = [
    "日本", "天皇", "幕府", "将軍", "藩", "朝廷", "大化", "倭", "皇后", "上皇",
    "信長", "秀吉", "家康", "武田", "上杉", "北条", "足利", "徳川", "豊臣", "織田",
    "源頼朝", "源義経", "平清盛", "平氏", "源氏", "藤原", "鎌倉", "室町", "江戸",
    "戦国", "関ヶ原", "維新", "薩摩", "長州", "土佐", "明治", "大正", "昭和", "平成",
    "令和", "京都", "大阪", "東京", "沖縄", "北海道", "蝦夷", "琉球", "首相", "内閣",
    "皇子", "蘇我", "中臣", "飛鳥", "奈良", "平安", "大名", "武士", "幕末", "神社",
]
ITEM_RE = re.compile(r"^(紀元前)?(\d{1,4})年(?:[（(][^）)]*[）)])?\s*[-–－―ー]\s*(.+)$")
HEADING_RE = re.compile(r"^(=+)\s*(.+?)\s*=+$")


def _fetch_extract(month: int, day: int) -> str:
    params = {"action": "query", "prop": "extracts", "explaintext": 1,
              "format": "json", "titles": f"{month}月{day}日", "redirects": 1}
    url = "https://ja.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as res:
        data = json.loads(res.read().decode("utf-8"))
    for page in data.get("query", {}).get("pages", {}).values():
        return page.get("extract", "") or ""
    return ""


def parse_extract(text: str):
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = HEADING_RE.match(line)
        if m:
            if len(m.group(1)) == 2:
                section = SECTION_MAP.get(m.group(2))
            continue
        if section is None:
            continue
        m = ITEM_RE.match(line.lstrip("*・ ").strip())
        if not m:
            continue
        bc, year, body = m.group(1), int(m.group(2)), m.group(3).strip()
        yield section, (f"紀元前{year}年" if bc else f"{year}年"), (-year if bc else year), body


def history_memo(year_num: int, is_japan: bool) -> str:
    notes = []
    if is_japan and 0 < year_num <= 1872:
        notes.append("日本は1872年まで旧暦。新暦換算の日付なので、他の資料と1日以上ズレることがある")
    if year_num < 1000:
        notes.append("古い年代は日付・年とも諸説あり")
    return " / ".join(notes)


def fetch_history(month: int, day: int):
    """(項目リスト, エラー文 or None) を返す"""
    label = f"{month}月{day}日"
    try:
        text = _fetch_extract(month, day)
    except Exception as e:  # ネットワークエラーなど
        return [], f"{label}の取得に失敗しました: {e}"
    url = "https://ja.wikipedia.org/wiki/" + urllib.parse.quote(label)
    items = []
    for kind, year_label, year_num, body in parse_extract(text):
        jp = any(w in body for w in JAPAN_WORDS)
        items.append({
            "label": label, "kind": kind, "year_label": year_label,
            "year_num": year_num, "body": body, "japan": jp,
            "memo": history_memo(year_num, jp), "url": url,
        })
    return items, (None if items else f"{label}の項目が見つかりませんでした")


def history_copy_text(item) -> str:
    return f"【{item['label']}は何の日】\n{item['year_label']}：{item['body']}\n出典: {item['url']}"


def history_rows(items) -> list:
    return [{
        "日付": i["label"], "区分": i["kind"], "年": i["year_label"], "内容": i["body"],
        "日本関連(推定)": "○" if i["japan"] else "", "要確認メモ": i["memo"], "出典URL": i["url"],
    } for i in items]
