"""ネタ収集ボード（世界情勢 / 中東 / 今日は何の日）"""
from collections import Counter
from datetime import datetime

import streamlit as st

import core

st.set_page_config(page_title="ネタ収集ボード", page_icon="📰", layout="centered")


def check_password() -> bool:
    """Secretsに APP_PASSWORD があるときだけ、パスワードを求める"""
    try:
        pw = st.secrets.get("APP_PASSWORD")
    except Exception:
        pw = None
    if not pw or st.session_state.get("authed"):
        return True
    entered = st.text_input("パスワード", type="password")
    if entered:
        if entered == pw:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("パスワードが違います")
    return False


@st.cache_data(ttl=1800, show_spinner="ニュースを取得中...")
def load_news(hours: int):
    return core.fetch_feeds(core.FEEDS, hours)


@st.cache_data(ttl=86400, show_spinner="Wikipediaから取得中...")
def load_history(month: int, day: int):
    return core.fetch_history(month, day)


HOURS_OPTIONS = [12, 24, 36, 48, 72]


TOPIC_TOP_N = 5


def render_topics(view, prefix: str):
    """🔥 複数媒体が報じている話題を、媒体数の多い順に表示"""
    topics = core.find_topics(view)[:TOPIC_TOP_N]
    st.subheader("🔥 報道の多い話題")
    st.caption("同じ話題を報じている媒体の数で並べた目安（閲覧数ではない）。"
               "英語記事どうしを単語の重なりで判定しているので、取りこぼしや混ざりもある")
    if not topics:
        st.info("いま複数の媒体が報じている話題は見つかりませんでした")
        return
    top = len(topics[0]["brands"])
    for n, t in enumerate(topics):
        with st.container(border=True):
            st.progress(len(t["brands"]) / top,
                        text=f"**{len(t['brands'])}媒体**・拠点{len(t['bases'])}つ"
                             f"（{'・'.join(t['bases'])}）")
            st.markdown(f"**{t['articles'][0]['title']}**")
            with st.expander(f"各媒体の見出し（{len(t['articles'])}件）"):
                for i in t["articles"]:
                    st.markdown(f"- [{i['title']}]({i['url']})")
                    st.caption(f"　{core.source_label(i)}・{core.pub_label(i)}"
                               + ("　⚠️当事国政府系" if i["party_state"] else ""))
                st.code(core.topic_copy_text(t), language=None)
    st.divider()


def render_news(items, errors, prefix: str):
    if errors:
        st.warning("取得できなかった媒体: " + "、".join(errors) + "（URL変更の可能性）")

    c1, c2 = st.columns(2)
    with c1:
        only_me = st.toggle("🕌 中東関連だけ", value=False, key=f"{prefix}_me")
    with c2:
        show_ps = st.toggle("当事国の政府系も表示", value=False, key=f"{prefix}_ps",
                            help="イラン（Tehran Times）・トルコ（Anadolu）の政府系メディア。"
                                 "当事国の公式見解を知る目的で使い、事実の裏取りには使わないのがおすすめ")
    if not show_ps:
        items = [i for i in items if not i["party_state"]]
    if only_me:
        items = [i for i in items if i["mideast"]]
        st.caption("同じ出来事を拠点の違う2媒体以上が報じているか確認してから使うのがおすすめ")
    if not items:
        st.info("該当する記事はありませんでした")
        return

    medias = sorted({i["media"] for i in items})
    with st.expander("絞り込み"):
        selected = st.multiselect("媒体", medias, default=medias, key=f"{prefix}_media")
        query = st.text_input("キーワード検索", key=f"{prefix}_q")
        show_img = st.checkbox("画像を表示", value=True, key=f"{prefix}_img",
                               help="外したら画像を読み込まないので、スマホの通信量を節約できる")

    view = [
        i for i in items
        if i["media"] in selected
        and (not query or query.lower() in (i["title"] + i["summary"]).lower())
    ]
    render_topics(view, prefix)

    bases = Counter(i["base"] for i in view)
    st.caption(f"{len(view)}件　" + " / ".join(f"{b} {n}" for b, n in bases.items()))
    name = "mideast" if only_me else prefix
    st.download_button(
        "CSVで保存", core.to_csv_bytes(core.news_rows(view), core.NEWS_COLS),
        file_name=f"{name}_{datetime.now(core.JST):%Y%m%d}.csv",
        mime="text/csv", key=f"{prefix}_dl",
    )

    for it in view:
        with st.container(border=True):
            if show_img and it["image"]:
                # URLをそのまま渡すと、画像はブラウザが媒体のサーバーから直接読み込む
                st.image(it["image"], width=320)
            st.markdown(f"**{it['title']}**")
            st.caption(f"{core.source_label(it)}・{core.pub_label(it)}"
                       + ("・🕌中東" if it["mideast"] else ""))
            if it["party_state"]:
                st.caption("⚠️ 紛争当事国の政府系メディア。公式見解として扱い、事実は他媒体で確認")
            if it["hits"]:
                st.caption("🔎 " + " / ".join(it["hits"]))
            if it["summary"]:
                st.write(it["summary"])
            st.markdown(f"[記事を開く]({it['url']})")
            with st.expander("コピー用テキスト"):
                st.code(core.news_copy_text(it), language=None)


def render_history():
    d = st.date_input("日付", value=datetime.now(core.JST).date(), key="hist_date")
    c1, c2 = st.columns(2)
    with c1:
        kinds = st.multiselect("区分", ["記念日", "できごと", "誕生", "死去"],
                               default=["記念日", "できごと"], key="hist_kinds")
    with c2:
        order = st.radio("並び順", ["古い順", "新しい順"], horizontal=True, key="hist_order")
    only_jp = st.checkbox("日本関連だけ", value=True, key="hist_jp",
                          help="キーワードと人名の形から推定。記念日は日本のものと国際デーなどを残す")

    items, err = load_history(d.month, d.day)
    if err:
        st.error(err)
        return

    view = [i for i in items if i["kind"] in kinds and (i["japan"] or not only_jp)]
    annivs = [i for i in view if i["kind"] == "記念日"]
    dated = sorted((i for i in view if i["kind"] != "記念日"),
                   key=lambda i: i["year_num"], reverse=(order == "新しい順"))
    st.caption(f"{len(view)}件（全{len(items)}件中）　記念日 {len(annivs)}・年のある項目 {len(dated)}")
    st.download_button(
        "CSVで保存", core.to_csv_bytes(core.history_rows(annivs + dated), core.HISTORY_COLS),
        file_name=f"rekishi_{d.month}-{d.day}.csv", mime="text/csv", key="hist_dl",
    )

    if annivs:
        st.subheader("🎌 記念日・年中行事")
        for it in annivs:
            with st.container(border=True):
                where = f"（{it['country']}）" if it["country"] else ""
                st.markdown(f"**{it['body']}**{where}")
                if it["desc"]:
                    st.write(it["desc"])
                if it["memo"]:
                    st.warning(it["memo"], icon="⚠️")
                with st.expander("コピー用テキスト"):
                    st.code(core.history_copy_text(it), language=None)

    if dated:
        st.subheader("📜 できごと・人物")
        for it in dated:
            with st.container(border=True):
                st.markdown(f"**{it['year_label']}**　{it['body']}")
                st.caption(f"{it['kind']}" + ("・日本関連(推定)" if it["japan"] else ""))
                if it["memo"]:
                    st.warning(it["memo"], icon="⚠️")
                with st.expander("コピー用テキスト"):
                    st.code(core.history_copy_text(it), language=None)

    st.caption("Wikipediaの文章はそのまま使わず、自分の言葉で書き直して、年と日付は別の資料でも確認してから投稿してね。")


def main():
    st.title("📰 ネタ収集ボード")
    if not check_password():
        return

    top_l, top_r = st.columns([3, 1])
    with top_l:
        st.caption(f"最終表示: {datetime.now(core.JST):%m/%d %H:%M}（日本時間）")
    with top_r:
        if st.button("🔄 更新"):
            st.cache_data.clear()
            st.rerun()

    tab_news, tab_history = st.tabs(["🌍 世界情勢・中東", "📅 今日は何の日"])

    with tab_news:
        hours = st.selectbox("対象期間", HOURS_OPTIONS, index=2,
                             format_func=lambda h: f"直近{h}時間", key="news_hours")
        items, errors = load_news(hours)
        render_news(items, errors, "news")

    with tab_history:
        render_history()


main()
