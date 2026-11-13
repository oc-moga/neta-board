# ネタ収集ボード

世界情勢・中東・今日は何の日のネタを、スマホでもPCでも見られるWebアプリです。

## 1. PCで動作確認（任意）
1. コマンドプロンプトでこのフォルダに移動
2. `py -m pip install -r requirements.txt`
3. `py -m streamlit run app.py`
4. ブラウザが開けばOK

## 2. クラウドに公開（スマホ・外出先から使う）
1. GitHubアカウントを作る（無料）
2. 新しいリポジトリを作る（Privateでも可）
3. 「Add file → Upload files」で、この4ファイルをアップロード
   （app.py / core.py / requirements.txt / README.md）
4. https://share.streamlit.io にGitHubでログイン
5. 「Create app」→ 作ったリポジトリを選ぶ → Main file path に `app.py` → Deploy
6. 公開後、アプリの「Settings → Secrets」に次を貼る（パスワードで保護）
   APP_PASSWORD = "好きなパスワード"
7. スマホでURLを開き、ブラウザの「ホーム画面に追加」でアプリっぽく使える

## メモ
- 使わない期間が続くとスリープします。開くと復帰ボタンが出ます
- 画面の「CSVで保存」で、その時点の一覧をダウンロードできます
- 投稿済みの記録は、このバージョンでは保存されません
