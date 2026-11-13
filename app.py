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
def load_world(hours: int):
    return core.fetch_feeds(core.FEEDS_WORLD, hours, apply_filter=False)


@st.cache_data(ttl=1800, show_spinner="中東ニュースを取得中...")
def load_mideast(hours: int):
    return core.fetch_feeds(core.FEEDS_MIDEAST, hours, apply_filter=True)


@st.cache_data(ttl=86400, show_spinner="Wikipediaから取得中...")
def load_history(month: int, day: int):
    return core.fetch_history(month, day)


HOURS_OPTIONS = [12, 24, 36, 48, 72]


def render_news(items, errors, prefix: str, show_hits: bool = False):
    if errors:
        st.warning("取得できなかった媒体: " + "、".join(errors) + "（URL変更の可能性）")
    if not items:
        st.info("該当する記事はありませんでした")
        return

    medias = sorted({i["media"] for i in items})
    with st.expander("絞り込み"):
        selected = st.multiselect("媒体", medias, default=medias, key=f"{prefix}_media")
        query = st.text_input("キーワード検索", key=f"{prefix}_q")

    view = [
        i for i in items
        if i["media"] in selected
        and (not query or query.lower() in (i["title"] + i["summary"]).lower())
    ]
    bases = Counter(i["base"] for i in view)
    st.caption(f"{len(view)}件　" + " / ".join(f"{b} {n}" for b, n in bases.items()))
    st.download_button(
        "CSVで保存", core.to_csv_bytes(core.news_rows(view), core.NEWS_COLS),
        file_name=f"{prefix}_{datetime.now(core.JST):%Y%m%d}.csv",
        mime="text/csv", key=f"{prefix}_dl",
    )

    for idx, it in enumerate(view):
        with st.container(border=True):
            st.markdown(f"**{it['title']}**")
            st.caption(f"{it['media']}（{it['base']}）・{core.pub_label(it)}")
            if show_hits and it["hits"]:
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
        kinds = st.multiselect("区分", ["できごと", "誕生", "死去"],
                               default=["できごと"], key="hist_kinds")
    with c2:
        order = st.radio("並び順", ["古い順", "新しい順"], horizontal=True, key="hist_order")
    only_jp = st.checkbox("日本関連だけ", value=True, key="hist_jp")

    items, err = load_history(d.month, d.day)
    if err:
        st.error(err)
        return

    view = [i for i in items if i["kind"] in kinds and (i["japan"] or not only_jp)]
    view.sort(key=lambda i: i["year_num"], reverse=(order == "新しい順"))
    st.caption(f"{len(view)}件（全{len(items)}件中）")
    st.download_button(
        "CSVで保存", core.to_csv_bytes(core.history_rows(view), core.HISTORY_COLS),
        file_name=f"rekishi_{d.month}-{d.day}.csv", mime="text/csv", key="hist_dl",
    )

    for it in view:
        with st.container(border=True):
            st.markdown(f"**{it['year_label']}**　{it['body']}")
            st.caption(f"{it['kind']}" + ("・日本関連" if it["japan"] else ""))
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

    tab_world, tab_mideast, tab_history = st.tabs(["🌍 世界情勢", "🕌 中東", "📅 今日は何の日"])

    with tab_world:
        hours = st.selectbox("対象期間", HOURS_OPTIONS, index=2,
                             format_func=lambda h: f"直近{h}時間", key="world_hours")
        items, errors = load_world(hours)
        render_news(items, errors, "world")

    with tab_mideast:
        hours = st.selectbox("対象期間", HOURS_OPTIONS, index=2,
                             format_func=lambda h: f"直近{h}時間", key="mideast_hours")
        items, errors = load_mideast(hours)
        st.caption("同じ出来事を拠点の違う2媒体以上が報じているか確認してから使うのがおすすめ")
        render_news(items, errors, "mideast", show_hits=True)

    with tab_history:
        render_history()


main()
