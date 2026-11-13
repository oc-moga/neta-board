"""ネタ収集のコアロジック（画面に依存しない部分）"""
import calendar
import csv
import io
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser

JST = ZoneInfo("Asia/Tokyo")
USER_AGENT = "neta-board/1.0 (personal use; SNS content research)"

# ===== RSS設定 =====
# filter=True の媒体は、中東タブのときだけキーワードに一致した記事に絞る
# base は媒体の拠点（裏取りで、拠点の違う媒体を見比べるための目印）
FEEDS_WORLD = [
    {"name": "NHK 国際", "base": "日本", "filter": False,
     "url": "https://www.nhk.or.jp/rss/news/cat6.xml"},
    {"name": "BBC World", "base": "英国", "filter": False,
     "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "Al Jazeera", "base": "カタール", "filter": False,
     "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "Guardian World", "base": "英国", "filter": False,
     "url": "https://www.theguardian.com/world/rss"},
]
FEEDS_MIDEAST = [
    {"name": "NHK 国際", "base": "日本", "filter": True,
     "url": "https://www.nhk.or.jp/rss/news/cat6.xml"},
    {"name": "BBC Middle East", "base": "英国", "filter": False,
     "url": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"},
    {"name": "Guardian Middle East", "base": "英国", "filter": False,
     "url": "https://www.theguardian.com/world/middleeast/rss"},
    {"name": "Al Jazeera", "base": "カタール", "filter": True,
     "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "Times of Israel", "base": "イスラエル", "filter": False,
     "url": "https://www.timesofisrael.com/feed/"},
]

KEYWORDS_JA = [
    "中東", "イラン", "イスラエル", "ガザ", "パレスチナ", "ハマス", "ヒズボラ",
    "レバノン", "シリア", "イエメン", "フーシ", "ホルムズ", "サウジ", "イラク",
    "カタール", "アラブ首長国", "オマーン", "ヨルダン川", "湾岸", "テヘラン",
]
KEYWORDS_EN = [
    "Middle East", "Iran", "Iranian", "Israel", "Israeli", "Gaza", "Palestin\\w*",
    "Hamas", "Hezbollah", "Lebanon", "Syria", "Yemen", "Houthi\\w*", "Hormuz",
    "Saudi", "Iraq", "Qatar", "Emirates", "Oman", "West Bank", "Tehran", "Gulf",
]
PATTERN = re.compile(
    "|".join(KEYWORDS_JA) + "|" + "|".join(rf"\b{k}\b" for k in KEYWORDS_EN),
    re.IGNORECASE,
)

NEWS_COLS = ["媒体", "拠点", "タイトル", "要約", "公開日時", "ヒットしたキーワード", "URL"]
HISTORY_COLS = ["日付", "区分", "年", "内容", "日本関連(推定)", "要確認メモ", "出典URL"]


def clean(text: str, limit: int = 200) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def find_hits(text: str) -> list:
    return sorted({m.group(0) for m in PATTERN.finditer(text)})


def fetch_feeds(feeds: list, hours: int, apply_filter: bool):
    """RSSを取得して (記事リスト, 取得失敗した媒体名のリスト) を返す"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items, errors, seen = [], [], set()

    for f in feeds:
        parsed = feedparser.parse(f["url"], agent=USER_AGENT)
        if parsed.bozo and not parsed.entries:
            errors.append(f["name"])
            continue
        for e in parsed.entries:
            link = e.get("link", "")
            if not link or link in seen:
                continue
            title = clean(e.get("title", ""), 150)
            summary = clean(e.get("summary", ""))
            hits = find_hits(f"{title} {summary}")
            if apply_filter and f.get("filter") and not hits:
                continue

            pub = e.get("published_parsed") or e.get("updated_parsed")
            pub_dt = None
            if pub:
                # feedparserの時刻はUTC。timegmで正しくUTCとして扱う
                pub_dt = datetime.fromtimestamp(calendar.timegm(pub), tz=timezone.utc)
                if pub_dt < cutoff:
                    continue
            seen.add(link)
            items.append({
                "media": f["name"], "base": f["base"], "title": title,
                "summary": summary, "pub_dt": pub_dt, "hits": hits, "url": link,
            })

    items.sort(key=lambda i: i["pub_dt"] or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    return items, errors


def pub_label(item) -> str:
    if not item["pub_dt"]:
        return "日時不明"
    return item["pub_dt"].astimezone(JST).strftime("%m/%d %H:%M")


def news_copy_text(item) -> str:
    return f"{item['title']}\n出典: {item['media']}（{item['base']}）\n{item['url']}"


def news_rows(items) -> list:
    return [{
        "媒体": i["media"], "拠点": i["base"], "タイトル": i["title"],
        "要約": i["summary"], "公開日時": pub_label(i),
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
