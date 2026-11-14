# -*- coding: utf-8 -*-
"""
歴史画像サーチ（画面付きアプリ）
image_collector.py と同じフォルダに置いて、次のコマンドで起動する：
    py -m pip install streamlit requests pillow
    py -m streamlit run app.py
"""
import csv
import os
import re
import urllib.parse

import streamlit as st

import image_collector as ic

st.set_page_config(page_title="歴史画像サーチ", layout="wide")

IMG_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".webp"}
CSV_HEADER = ["題材", "ヒットしたキーワード", "タイトル", "保存ファイル", "画像URL", "掲載元ページURL",
              "所蔵機関", "データベース", "権利区分", "クレジット", "画像区分",
              "元画像サイズ", "縦横比", "16:9向き", "時代", "緩和レベル", "同じ画像が載っているページ"]


def preview_url(c):
    """プレビュー用に軽い画像URLへ変換（失敗したら原画像のまま）"""
    u = c["image_url"]
    if c["source"] == "jps":
        return re.sub(r"/full/(max|full)/0/default\.jpg$", "/full/800,/0/default.jpg", u)
    if c["source"] == "commons":
        name = urllib.parse.quote(c["title"].replace(" ", "_"))
        return f"https://commons.wikimedia.org/wiki/Special:FilePath/{name}?width=800"
    if c["source"] == "met":
        return u.replace("/original/", "/web-large/")
    return u


def esc(t):
    return re.sub(r"([*_\[\]()#`<>~|])", r"\\\1", t or "")


def merge(a, b):
    out = list(a)
    for t in b:
        if t not in out:
            out.append(t)
    return out


# ------------------------------ 検索フォーム ------------------------------
st.title("歴史画像サーチ")
syn = ic.load_synonyms()

with st.form("search"):
    topic = st.text_input("題材（例：黒船来航）")
    c1, c2 = st.columns(2)
    jp_extra = c1.text_input("日本語の別表記（; 区切り・任意）", placeholder="米艦;ペリー;浦賀")
    en_extra = c2.text_input("英語キーワード（; 区切り・任意）", placeholder="Perry Expedition;Black Ships")
    kind = st.selectbox(
        "画像の種類で絞り込む", list(ic.KIND_FILTERS),
        help="タイトルや分類に「肖像」「portrait」などの言葉が入っている画像だけ残す。"
             "絞るほど件数は減るので、少ないときは英語キーワードを足すと増えやすい")
    c3, c4 = st.columns(2)
    target = c3.slider("表示する枚数", 5, 40, 12)
    per_source = c4.slider("取得元ごとの候補上限", 4, 20, 10)
    submitted = st.form_submit_button("検索", type="primary")

if submitted and topic.strip():
    topic = topic.strip()
    base = syn.get(topic, {"jp": [], "en": []})
    tmp = {topic: {
        "jp": merge(base["jp"], ic.split_terms(jp_extra)),
        "en": merge(base["en"], ic.split_terms(en_extra)),
    }}
    ic.PER_SOURCE_LIMIT = per_source
    with st.spinner("検索中…（30秒〜1分くらいかかることがあるよ）"):
        results = ic.collect(topic, tmp, target, kind)
    for k in list(st.session_state.keys()):
        if k.startswith("sel_"):
            del st.session_state[k]
    st.session_state.update(results=results, topic=topic, tmp=tmp, saved=None, kind=kind)

# ------------------------------ 結果表示 ------------------------------
results = st.session_state.get("results")
if results is not None:
    topic = st.session_state["topic"]
    kind = st.session_state.get("kind", "すべて")
    kind_label = "" if kind == "すべて" else f"（{kind}）"
    st.subheader(f"「{topic}」{kind_label} {len(results)}件")

    if not results:
        msg = "見つからなかった。別表記や英語キーワードを足して、もう一度検索してみて"
        if kind != "すべて":
            msg += "。種類を「すべて」に戻すと出てくるかも"
        st.warning(msg)
    else:
        b1, b2, _ = st.columns([1, 1, 6])
        if b1.button("すべて選択"):
            for i in range(len(results)):
                st.session_state[f"sel_{i}"] = True
        if b2.button("選択解除"):
            for i in range(len(results)):
                st.session_state[f"sel_{i}"] = False

        N = 3
        for start in range(0, len(results), N):
            cols = st.columns(N)
            for j, c in enumerate(results[start:start + N]):
                i = start + j
                with cols[j]:
                    st.image(preview_url(c), use_container_width=True)
                    st.markdown(f"**{esc(c['title'][:60]) or '(無題)'}**")
                    sz, ratio, fit = ic.size_info(c["width"], c["height"])
                    st.caption(
                        f"{esc(c['provider'])} ／ 権利: {esc(c['rights'])} ／ サイズ: {sz} ／ "
                        f"16:9: {fit} ／ ヒット語: {esc(c['keyword'])} ／ {esc(c['level'])}"
                    )
                    if c["credit"] and c["source"] == "commons":
                        st.caption(f"クレジット: {esc(c['credit'][:60])}")
                    if c["period"]:
                        st.caption(f"時代: {esc(c['period'])}")
                    if c["page_url"]:
                        st.markdown(f"[出典ページを開く]({c['page_url']})")
                    others = c.get("other_pages", [])
                    total = max(c.get("other_pages_total", 0), len(others))
                    label = f"同じ画像が載っているページ（{total}件）" if total else "同じ画像が載っているページを探す"
                    with st.expander(label):
                        for p in others:
                            st.markdown(f"- [{esc(p['label'][:50])}]({p['url']})")
                        if total > len(others):
                            st.caption(f"ほか {total - len(others)} 件（多いので先頭だけ表示）")
                        st.caption("Web全体から探す：")
                        st.markdown(" ／ ".join(
                            f"[{p['label']}]({p['url']})" for p in ic.reverse_search_links(c["image_url"])))
                    st.checkbox("これを使う", key=f"sel_{i}")

        # ------------------------------ 保存 ------------------------------
        selected = [i for i in range(len(results)) if st.session_state.get(f"sel_{i}")]
        st.divider()
        st.write(f"選択中：{len(selected)}件")

        if st.button("選択した画像をPCに保存", disabled=not selected, type="primary"):
            folder = ic.OUTPUT_DIR / ic.safe_name(topic)
            folder.mkdir(parents=True, exist_ok=True)
            existing = len([p for p in folder.iterdir() if p.suffix.lower() in IMG_EXTS])

            rows, saved, errors = [], [], []
            bar = st.progress(0.0)
            for n, i in enumerate(selected, 1):
                c = results[i]
                path, err = ic.download(c, folder, existing + n)
                if path:
                    c["file"] = path.name
                    saved.append(path.name)
                else:
                    errors.append(f"{c['title'][:30]}：{err}")
                sz, ratio, fit = ic.size_info(c["width"], c["height"])
                rows.append([
                    topic, c["keyword"], c["title"], c["file"] or f"DL失敗({err})", c["image_url"],
                    c["page_url"], c["provider"], c["database"], c["rights"], c["credit"], c["kind"],
                    sz, ratio, fit, c["period"], c["level"], ic.other_pages_text(c),
                ])
                bar.progress(n / len(selected))

            csv_path = folder / "results.csv"
            is_new = not csv_path.exists()
            with open(csv_path, "a", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                if is_new:
                    w.writerow(CSV_HEADER)
                w.writerows(rows)

            ic.update_hits(topic, [results[i] for i in selected])
            st.session_state["saved"] = {"folder": str(folder), "files": saved, "errors": errors}

        saved = st.session_state.get("saved")
        if saved:
            st.success(f"{len(saved['files'])}件を保存した：{saved['folder']}")
            if saved["errors"]:
                st.error("保存できなかったもの：\n" + "\n".join(saved["errors"]))
            if st.button("保存先フォルダを開く"):
                try:
                    os.startfile(saved["folder"])  # Windows専用
                except Exception as e:  # noqa: BLE001
                    st.info(f"自動で開けなかった。手動で開いてね：{e}")

    # ------------------------------ 辞書に保存 ------------------------------
    with st.expander("今回使った別表記を辞書に保存する"):
        tmp = st.session_state.get("tmp", {})
        entry = tmp.get(topic, {"jp": [], "en": []})
        st.write("日本語：" + (" / ".join(entry["jp"]) or "なし"))
        st.write("英語：" + (" / ".join(entry["en"]) or "なし"))
        if st.button("synonyms.csv に保存"):
            d = ic.load_synonyms()
            cur = d.setdefault(topic, {"jp": [], "en": []})
            cur["jp"] = merge(cur["jp"], entry["jp"])
            cur["en"] = merge(cur["en"], entry["en"])
            ic.save_synonyms(d)
            st.success("辞書に保存した。次回から自動で使われる")
