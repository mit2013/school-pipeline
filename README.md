# school-pipeline

私立中学校の学校行事・小テスト・定期試験・課題提出などの予定を、メール(Gmail)・
配布プリント(PDF/写真)・Classroom投稿のコピペテキストから自動抽出し、
Googleカレンダーへ登録するパイプラインです。

## 課題意識

- 学校共通アドレスと専門コース用アドレスでメールが分散している
- 教員によって Classroom に登録する人・メールだけの人・週末に金曜締切を連絡する人がバラバラ
- 紙の手帳は書き込まれない
- 結果、子どもが自分の予定を把握できず、ノー勉で小テストに臨む/試験直前に情報整理から始める、という状態になっている

このツールは「予定の存在に気づく」までの確認コストを下げることが目的です。
勉強するかどうかは本人次第ですが、情報を集める作業自体は自動化できます。

## 全体の流れ

```
[Gmail(複数アカウント)]
[配布プリントPDF]         →  抽出(Claude)  →  重複整理  →  確認(dry-run)  →  Googleカレンダー
[写真(OCR)]
[Classroom投稿のコピペ]
```

1. **取得**: Gmail・PDF・写真(OCR)・手動コピペテキストから本文を集める
2. **抽出**: Claude(Anthropic API)に本文を渡し、種別(行事/小テスト/定期試験/課題/その他)・日時・科目などを構造化データとして抽出する。同時に確信度(confidence)も出力させる
3. **重複整理**: 同じ予定を指す複数の情報源(担任のメール＋教科プリントなど)を1件に統合し、似ているが別物かもしれない予定は「要確認」として一覧に出す(自動統合はしない)
4. **確認**: デフォルトはドライラン。カレンダーに登録される予定・更新される予定・確信度が低く除外された予定を一覧表示する
5. **登録**: `--push` を付けたときだけ実際にGoogleカレンダーへ書き込む。再実行しても重複登録されず、内容が変わった予定だけ更新される(冪等)

## セットアップ

### 1. インストール

Python 3.10 以上が必要です。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

OCR(スキャンPDF・写真)を使う場合は、別途 Tesseract OCR 本体と日本語データが必要です。

```bash
# Debian/Ubuntu の例
sudo apt-get install tesseract-ocr tesseract-ocr-jpn
# macOS の例
brew install tesseract tesseract-lang
```

### 2. Google Cloud の準備 (Gmail API / Calendar API)

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成
2. 「Gmail API」と「Google Calendar API」を有効化
3. 「認証情報」→「OAuthクライアントID」→ アプリケーションの種類は「デスクトップアプリ」で作成
4. ダウンロードしたJSONを、学校共通アドレス用・専門コースアドレス用・カレンダー用としてそれぞれ `secrets/` 以下に保存
   (Gmailの2アカウントは別々のGoogleアカウントなので、OAuthクライアント自体は共用しても、認証(トークン取得)はアカウントごとに個別に行います)

`secrets/` と `config.yaml` は `.gitignore` 済みです。認証情報や取得したメール本文をリポジトリにコミットしないでください。

### 3. Anthropic APIキー

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### 4. 設定ファイル

```bash
cp config.example.yaml config.yaml
```

`config.yaml` を開き、`gmail_accounts` の `credentials_path`、`pdf_directory` などを環境に合わせて編集してください。
使わない取得元(例: 写真OCRを使わない)は該当行を削除して構いません。

### 5. 初回認証

Gmailアカウントごと、カレンダーそれぞれで一度だけブラウザ認証が必要です。

```bash
school-pipeline auth-gmail --account school_common
school-pipeline auth-gmail --account course_specific
school-pipeline auth-calendar
```

## 使い方

### 1. まずドライランで確認する

```bash
school-pipeline run
```

抽出された予定の一覧、確信度が低くて除外された予定、重複の可能性がある予定、
カレンダーに対する差分(新規/更新/変更なし件数)が表示されます。実際の書き込みは行われません。

### 2. 問題なければ実際に登録する

```bash
school-pipeline run --push
```

### 3. Classroomの投稿だけは手動で

ClassroomはAPI連携していないため、教員がClassroomにしか投稿しない情報は自動取得できません。
運用でカバーする場合は、投稿本文をコピーして `inbox/manual/` に `.txt` として保存するだけで、
次回の `run` から自動的に取り込まれます。

配布プリントをスマホで撮影した場合は `inbox/photos/` に、PDFでもらった場合は `inbox/pdfs/` に置いてください。

### 4. 定期実行する

cron で毎朝実行する例(ドライラン結果をメールで確認し、問題なければ手動で `--push` する運用を推奨):

```cron
0 7 * * * cd /path/to/school-pipeline && .venv/bin/school-pipeline run >> run.log 2>&1
```

慣れてきたら `run --push` を直接スケジュールしても構いませんが、抽出ミスに気づけるよう、
最初のうちはドライラン結果を毎回確認することを推奨します。

## 精度・安全性についての考え方

- LLMによる抽出には誤り(誤読・日付の取り違え)がありえます。**確信度が低い予定は自動的にカレンダー登録から除外され、一覧表示のみ**されます(`confidence_threshold` で調整可能)
- 似た予定が別ソースから複数出てきた場合、機械的に断定して統合すると誤って別の予定を1つに潰してしまう危険があるため、**重複候補は一覧に出すだけで自動統合しません**
- カレンダー同期は `extendedProperties` に予定の識別子を保存して行うため、**同じ予定を何度実行しても重複登録されません**。内容が変わった場合のみ既存の予定を更新します
- 誤って登録された予定を消す自動削除機能は今回実装していません(消し過ぎのリスクを避けるため)。誤りに気づいた場合はGoogleカレンダー側で手動削除してください

## テスト

外部API(Anthropic / Google)を実際に呼び出さず、フェイクのクライアント/サービスに差し替えてロジックを検証しています。

```bash
pip install -e ".[dev]"
pytest
```

## 今後の拡張案

- Google Classroom API連携(対応教科すべてで使われていれば有効。今回は「使う教員/使わない教員が混在」という前提のため見送り)
- 抽出結果のLINE通知・週次サマリー送信
- 誤登録に気づいたときの取り消し(前回実行との差分から自動アーカイブ)
- 子ども向けの「今日・今週やること」ダッシュボード
