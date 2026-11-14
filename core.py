"""ネタ収集のコアロジック（画面に依存しない部分）"""
import calendar
import csv
import gzip
import io
import json
import re
import urllib.parse
import socket
import urllib.error
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
    # Newsmax はRSSが応答しない（ボット対策の可能性）ため外した。代わりに米保守寄りの国際面
    {"name": "New York Post World", "base": "米国", "kind": "民間", "mideast": False,
     "url": "https://nypost.com/world-news/feed/"},
    {"name": "CBN News World", "base": "米国", "kind": "宗教系(キリスト教)", "mideast": False,
     "url": "https://www1.cbn.com/app_feeds/rss/news/rss.php?section=world"},
    {"name": "CBN News Israel", "base": "米国", "kind": "宗教系(キリスト教)", "mideast": True,
     "url": "https://www1.cbn.com/app_feeds/rss/news/rss.php?section=israel"},
    # NPR World はStreamlit Cloudから取得できないため外し、同じ米公共メディアのPBSに替えた
    {"name": "PBS NewsHour World", "base": "米国", "kind": "公共放送", "mideast": False,
     "url": "https://www.pbs.org/newshour/feeds/rss/world"},
    # --- 中東 ---
    {"name": "Al Jazeera", "base": "カタール", "kind": "政府出資", "mideast": False,
     "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "The National", "base": "UAE", "kind": "王族系資本", "mideast": True,
     "url": "https://www.thenationalnews.com/arc/outboundfeeds/rss/category/news/mena/?outputType=xml"},
    # Times of Israel・Tehran Times はStreamlit Cloudから取得できないため外した
    # （イランの媒体はクラウドから届かないものが多く、Mehr News も同じ配信網のため見送り）
    {"name": "Haaretz", "base": "イスラエル", "kind": "民間", "mideast": True,
     "url": "https://www.haaretz.com/srv/haaretz-latest-headlines"},
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


IMG_TAG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)


def extract_image(e) -> str:
    """記事の見出し画像URLを探す。媒体によって入っている場所が違うので順番に見る"""
    def ok(url):
        return isinstance(url, str) and url.startswith(("http://", "https://"))

    # 1) media:content（Guardian・Foxなど）。サイズ違いが複数あるときは一番大きいもの
    medias = [m for m in e.get("media_content", [])
              if ok(m.get("url")) and m.get("medium", "image") == "image"
              and (m.get("type") or "image").startswith("image")]
    if medias:
        return max(medias, key=lambda m: int(m.get("width") or 0))["url"]
    # 2) media:thumbnail（BBC・France 24など）
    for m in e.get("media_thumbnail", []):
        if ok(m.get("url")):
            return m["url"]
    # 3) 添付ファイル（enclosure）の画像（Tehran Times・UN Newsなど）
    for link in e.get("links", []):
        if (link.get("type") or "").startswith("image") and ok(link.get("href")):
            return link["href"]
    # 4) 本文HTMLの中の最初の<img>（NPR・Times of Israelなど）
    html = (e.get("summary") or "") + "".join(c.get("value", "") for c in e.get("content", []))
    m = IMG_TAG_RE.search(html)
    if m and ok(m.group(1)):
        return m.group(1)
    return ""


def find_hits(text: str) -> list:
    return sorted({m.group(0) for m in PATTERN.finditer(text)})


def _fetch_one(f: dict):
    """1媒体ぶんを取得してパース。(parsed, None) か、失敗したら (None, 理由) を返す"""
    try:
        req = urllib.request.Request(f["url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as res:
            data = res.read()
            ctype = res.headers.get("Content-Type", "")
        if data[:2] == b"\x1f\x8b":  # 頼んでいなくてもgzipで返す媒体がある（UN Newsなど）
            data = gzip.decompress(data)
        parsed = feedparser.parse(data)
    except urllib.error.HTTPError as e:
        # 403 = アクセス拒否（クラウドのサーバーからの接続を弾いている可能性）、404 = URLが無い
        return None, f"HTTP {e.code}"
    except (TimeoutError, socket.timeout):
        return None, f"{FETCH_TIMEOUT}秒以内に応答なし"
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return None, f"{FETCH_TIMEOUT}秒以内に応答なし"
        return None, f"接続エラー（{reason}）"
    except Exception as e:
        return None, f"{type(e).__name__}"
    if parsed.bozo and not parsed.entries:
        return None, f"RSSとして読めない中身（{ctype or '種類不明'}）"
    return parsed, None


def fetch_feeds(feeds: list, hours: int):
    """RSSを取得して (記事リスト, 取得失敗した媒体名のリスト) を返す
    各記事の "mideast" は中東関連かどうか（専門フィード由来 or キーワード一致）"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items, errors, seen = [], [], {}

    # 媒体数が多いので並列で取得（結果は FEEDS の並び順のまま処理する）
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_fetch_one, feeds))

    for f, (parsed, reason) in zip(feeds, results):
        if parsed is None:
            errors.append(f"{f['name']}（{reason}）")
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
                "hits": hits, "url": link, "image": extract_image(e),
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


# ===== 報道の多い話題（注目度の目安） =====
# 閲覧数はRSSに入っていないので、「同じ話題を何媒体が報じているか」を目安にする。
# タイトル＋要約の単語の重なりで判定する簡易版。言い回しが違うと取りこぼすことがあり、
# 英語記事どうしだけを比べる（日本語のNHKは対象外）。
STOPWORDS = set("""
the a an and or of to in on for with as at by from is are was were be been has have had
its it this that these those after before over into about amid says said say new more than
his her their they he she we you will would could can may not but who what when where why
how all also just one two three first last year years week day days people news live latest
update updates report reports watch video
""".split())
TOPIC_MIN_SHARED = 4      # 共通する単語がこの数以上
TOPIC_MIN_SIMILARITY = 0.18  # 単語の重なり具合（0〜1）がこの値以上なら同じ話題とみなす
FEED_SECTIONS = (" Middle East", " World", " 国際")


def brand(media: str) -> str:
    """同じ会社の別フィードを1媒体として数えるための名前（BBC World と BBC Middle East → BBC）"""
    if media.startswith("CBN News"):
        return "CBN News"
    for s in FEED_SECTIONS:
        if media.endswith(s):
            return media[: -len(s)]
    return media


def _topic_words(text: str) -> set:
    words = re.findall(r"[A-Za-z][A-Za-z'\-]+", text)
    return {w.lower().strip("'-") for w in words
            if len(w) >= 3 and w.lower() not in STOPWORDS}


def _is_english(title: str) -> bool:
    return bool(re.search(r"[A-Za-z]{4}", title)) and not re.search(
        r"[぀-ヿ一-鿿]", title)


def find_topics(items: list, min_brands: int = 2) -> list:
    """複数媒体が報じている話題をまとめ、媒体数の多い順に返す
    各話題: {"articles": [...], "brands": [...], "bases": [...]}"""
    arts = [i for i in items if _is_english(i["title"])]
    words = [_topic_words(f"{i['title']} {i['summary']}") for i in arts]

    parent = list(range(len(arts)))

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(len(arts)):
        for b in range(a + 1, len(arts)):
            if brand(arts[a]["media"]) == brand(arts[b]["media"]):
                continue  # 同じ会社どうしは比べない（別媒体の裏付けにならないので）
            shared = len(words[a] & words[b])
            if shared >= TOPIC_MIN_SHARED and \
                    shared / (len(words[a] | words[b]) or 1) >= TOPIC_MIN_SIMILARITY:
                parent[root(a)] = root(b)

    groups = {}
    for idx, art in enumerate(arts):
        groups.setdefault(root(idx), []).append(art)

    topics = []
    for g in groups.values():
        brands = sorted({brand(i["media"]) for i in g})
        if len(brands) < min_brands:
            continue
        g.sort(key=lambda i: i["pub_dt"] or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
        topics.append({"articles": g, "brands": brands,
                       "bases": sorted({i["base"] for i in g})})
    topics.sort(key=lambda t: (len(t["brands"]), len(t["bases"]), len(t["articles"])),
                reverse=True)
    return topics


def topic_copy_text(topic) -> str:
    lines = [f"【{len(topic['brands'])}媒体が報道／拠点: {'・'.join(topic['bases'])}】"]
    for i in topic["articles"]:
        note = "　※紛争当事国の政府系メディア" if i["party_state"] else ""
        lines.append(f"・{i['title']}\n  出典: {source_label(i)}・{pub_label(i)}{note}\n  {i['url']}")
    return "\n".join(lines)


def to_csv_bytes(rows: list, columns: list) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


# ===== 今日は何の日 =====
SECTION_MAP = {"できごと": "できごと", "誕生日": "誕生", "忌日": "死去", "記念日・年中行事": "記念日"}
JAPAN_WORDS = [
    "日本", "天皇", "幕府", "将軍", "藩", "朝廷", "大化", "倭", "皇后", "上皇",
    "信長", "秀吉", "家康", "武田", "上杉", "北条", "足利", "徳川", "豊臣", "織田",
    "源頼朝", "源義経", "平清盛", "平氏", "源氏", "藤原", "鎌倉", "室町", "江戸",
    "戦国", "関ヶ原", "維新", "薩摩", "長州", "土佐", "明治", "大正", "昭和", "平成",
    "令和", "京都", "大阪", "東京", "沖縄", "北海道", "蝦夷", "琉球", "内閣",
    "皇子", "蘇我", "中臣", "飛鳥", "奈良", "平安", "大名", "武士", "幕末", "神社",
    # 地名・役所・近代史（「首相」は「インド首相」なども拾ってしまうので外した）
    "県", "皇居", "皇室", "宮内", "国会", "衆議院", "参議院", "帝国議会", "内閣総理大臣",
    "文相", "蔵相", "外相", "通産省", "文部省", "大蔵省", "自衛隊", "国鉄", "地方裁判所",
    "戊辰", "西南戦争", "日清", "日露", "大本営", "連合艦隊", "寺",
]
# 人物の「、」の後ろにこれがあれば日本人ではないとみなす（漢字名の中国・朝鮮の人物など）
FOREIGN_PERSON_HINTS = ["中国", "中華", "清の", "清朝", "韓国", "朝鮮", "台湾", "香港",
                        "満洲", "モンゴル", "ベトナム", "皇帝", "王朝"]
# 「1690年（元禄3年10月14日） - 本文」の形。かっこの中（和暦など）も取り出す
ITEM_RE = re.compile(r"^(紀元前)?(\d{1,4})年\s*(?:[（(]([^）)]*)[）)])?\s*[-–－―ー]\s*(.+)$")
HEADING_RE = re.compile(r"^(=+)\s*(.+?)\s*=+$")
ANNIV_NAME_RE = re.compile(r"^(.+?)\s*[（(]\s*([^）)]+?)\s*[）)]$")


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
    """Wikipediaの本文から項目を取り出す
    できごと・誕生・死去: {"kind", "year", "bc", "paren"(かっこ内の和暦など), "body"}
    記念日: {"kind", "body"(名前), "country", "desc"(説明)}"""
    section = None
    anniv = None  # 説明を書き足している途中の記念日
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = HEADING_RE.match(line)
        if m:
            if len(m.group(1)) == 2:  # 「=== 人物 ===」など小見出しでは区分を変えない
                section = SECTION_MAP.get(m.group(2))
            continue
        if section is None:
            continue

        if section == "記念日":
            # 記念日は「名前（ 国）」の行のあとに説明の行が続く。句点のない短い行を名前とみなす
            if "。" not in line and len(line) <= 50:
                if anniv:
                    yield anniv
                nm = ANNIV_NAME_RE.match(line)
                anniv = {"kind": "記念日", "body": nm.group(1) if nm else line,
                         "country": nm.group(2) if nm else "", "desc": ""}
            elif anniv:
                anniv["desc"] += line
            continue

        m = ITEM_RE.match(line.lstrip("*・ ").strip())
        if m:
            yield {"kind": section, "bc": bool(m.group(1)), "year": int(m.group(2)),
                   "paren": (m.group(3) or "").strip(), "body": m.group(4).strip()}
    if anniv:
        yield anniv


def is_japan_related(entry: dict) -> bool:
    """日本関連かどうかの推定（キーワード判定なので完全ではない）"""
    if entry["kind"] == "記念日":
        # 日本の記念日（「埼玉県」など地域名の場合も含む）と、国の指定がないもの（国際デーなど）
        c = entry["country"]
        return c in ("", "世界") or any(w in c for w in JAPAN_WORDS)
    body = entry["body"]
    if any(w in body for w in JAPAN_WORDS):
        return True
    if entry["kind"] in ("誕生", "死去"):
        # 「名前、職業」の名前が漢字・ひらがなだけで、外国を示す語がなければ日本人とみなす
        name, _, rest = body.partition("、")
        if re.fullmatch(r"[一-鿿々぀-ゟ]{2,8}", name.replace(" ", "")) \
                and not any(h in rest for h in FOREIGN_PERSON_HINTS):
            return True
    return False


def history_memo(entry: dict, is_japan: bool) -> str:
    notes = []
    if entry["kind"] == "記念日":
        if any(w in entry["desc"] for w in ("株式会社", "協会", "組合", "連合会", "委員会")):
            notes.append("団体・企業が制定した記念日。由来は制定元の公式情報でも確認")
        return " / ".join(notes)
    year_num = -entry["year"] if entry["bc"] else entry["year"]
    if is_japan and 0 < year_num <= 1872:
        wareki = f"（旧暦では{entry['paren']}）" if re.search(r"\d+月\d+日", entry["paren"]) else ""
        notes.append(f"日本は1872年まで旧暦。この日付は新暦に換算したもの{wareki}。"
                     "旧暦の日付で記念日にしている資料もあるので注意")
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
    for e in parse_extract(text):
        jp = is_japan_related(e)
        if e["kind"] == "記念日":
            year_label, year_num = "", 0
        else:
            year_num = -e["year"] if e["bc"] else e["year"]
            year_label = f"紀元前{e['year']}年" if e["bc"] else f"{e['year']}年"
            if e["paren"]:
                year_label += f"（{e['paren']}）"  # 和暦などWikipediaのかっこ書きを残す
        items.append({
            "label": label, "kind": e["kind"], "year_label": year_label,
            "year_num": year_num, "body": e["body"], "desc": e.get("desc", ""),
            "country": e.get("country", ""), "japan": jp,
            "memo": history_memo(e, jp), "url": url,
        })
    return items, (None if items else f"{label}の項目が見つかりませんでした")


def history_copy_text(item) -> str:
    """投稿の下書き。Wikipediaの文のままなので、書き換え前提であることを最後に明記する"""
    if item["kind"] == "記念日":
        where = f"（{item['country']}）" if item["country"] else ""
        lines = [f"【{item['label']}は何の日】", f"{item['body']}{where}"]
        if item["desc"]:
            lines.append(item["desc"])
    else:
        lines = [f"【{item['label']}は何の日】", f"{item['year_label']}：{item['body']}"]
    if item["memo"]:
        lines.append(f"⚠️要確認: {item['memo']}")
    lines.append(f"出典: Wikipedia「{item['label']}」{item['url']}")
    lines.append("――（下書き：文は自分の言葉に書き換え、年・日付は別の資料でも確認）")
    return "\n".join(lines)


def history_rows(items) -> list:
    return [{
        "日付": i["label"], "区分": i["kind"], "年": i["year_label"],
        "内容": i["body"] + (f"（{i['country']}）" if i["country"] else "")
                + (f"：{i['desc']}" if i["desc"] else ""),
        "日本関連(推定)": "○" if i["japan"] else "", "要確認メモ": i["memo"], "出典URL": i["url"],
    } for i in items]
