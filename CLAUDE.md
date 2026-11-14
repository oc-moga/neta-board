# CLAUDE.md

ネタ収集ボード：世界情勢・中東ニュース（RSS）と「今日は何の日」（日本語版Wikipedia）を一覧表示する Streamlit アプリ。SNS投稿（YouTubeショート / X）のネタ探し用。Streamlit Community Cloud に公開してスマホからも使う。

## 起動（Windows）
```
py -m pip install -r requirements.txt
py -m streamlit run app.py
```
テストコードはない。動作確認は起動して画面で行う。

## 構成
- `app.py` … 画面（Streamlit）。パスワード確認、タブ表示、絞り込み、CSV保存、コピー用テキスト
- `core.py` … 画面に依存しないロジック。RSS取得・中東判定・Wikipedia取得/解析・CSV変換
  - Streamlit を import しない（ロジックは core.py、表示は app.py に分ける方針を守る）
- `requirements.txt` … streamlit / feedparser / tzdata（tzdata は Windows とクラウドで `Asia/Tokyo` を使うため）

## 主な仕組み
- **RSS**：`core.FEEDS` に媒体を定義。項目は `name` / `base`（拠点）/ `kind`（運営形態）/ `mideast` / `party_state` / `url`
  - `mideast=True` は中東専門フィード（全記事を中東扱い）。False ならキーワード（`KEYWORDS_JA` / `KEYWORDS_EN`）一致で判定
  - `party_state=True` は紛争当事国の政府系メディア（Anadolu）。初期状態では非表示
  - Streamlit Cloud（米国のサーバー）からは取れない媒体がある（NPR, Times of Israel, Tehran Times を外した。イランの媒体は配信網ごと届かないことが多い）。手元で取れてもクラウドで確認する
  - `kind` は資本・運営の事実で書く（立場の評価は書かない）
  - 8並列で取得、1媒体15秒タイムアウト、1媒体最大15件。失敗した媒体は画面に警告表示
  - 同じURLの記事は重複排除し、中東扱いは引き継ぐ
  - gzipで返す媒体（UN Newsなど）があるので手動で解凍している
- **今日は何の日**：Wikipedia API の `extracts`（プレーンテキスト）を取得し、見出し「できごと / 誕生日 / 忌日」の下の「〇〇年 - 内容」行を正規表現で抜き出す
  - 日本関連は `JAPAN_WORDS` の単語が含まれるかで推定
  - 1872年以前の日本関連は旧暦の注意、1000年より前は「諸説あり」のメモを付ける
- **キャッシュ**：ニュース30分、Wikipedia 24時間（`st.cache_data`）。画面の「更新」ボタンでクリア
- **時刻**：表示はすべて日本時間（`core.JST`）。feedparser の時刻はUTCなので `calendar.timegm` で変換
- **パスワード**：Streamlit の Secrets に `APP_PASSWORD` があるときだけ入力を求める（ローカルでは通常なし）

## 注意
- 裏取りを助ける表示（拠点・運営形態・政府系の警告・Wikipediaは書き直す旨の注意書き）は消さない
- 媒体を追加・変更したら、実際に起動して「取得できなかった媒体」に出ないか確認する
- 投稿済みの記録などの保存機能は今はない（クラウドはスリープ・再起動でデータが消えるため）
- 新しいライブラリを入れたら `requirements.txt` にも追加する（クラウド側はこれを見てインストールする）
