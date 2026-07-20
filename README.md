# RSS2Webhook
RSS Feed send to Webhook(Discord) / Made with Claude AI

# RSS -> Discord 通知Bot

RSSフィードを定期的にチェックし、新着記事および更新された記事をDiscordのWebhookに投稿するBotです。
配信済みの記事はSQLiteデータベースに記録され、内容の変化(タイトル・概要・リンク)をハッシュ値で検知します。

## ファイル構成

```
rss-discord-bot/
├── rss_discord_bot.py    # Bot本体(1回実行して終了するバッチスクリプト)
├── config.yaml.example   # 設定ファイルのサンプル
├── requirements.txt      # 依存パッケージ
└── README.md
```

実行するとカレントディレクトリ配下に以下が自動生成されます(config.yamlのパスに従う)。
- `data/rss_discord_bot.db` … 配信済み記事のデータベース
- `logs/rss_discord_bot.log` … ログファイル(自動ローテーション、5MB×5世代)
- `rss_discord_bot.lock` … 多重起動防止用ロックファイル

---

## 1. さくらのVPS (CentOS) でのセットアップ

### 1-1. Python3のインストール

CentOS 7の場合はPython3が標準では入っていないことが多いので、EPELリポジトリ経由で導入します。
CentOS 8/Stream系ではデフォルトで`python3`が使えます。

```bash
# CentOS 7の場合
sudo yum install -y epel-release
sudo yum install -y python3 python3-pip

# CentOS 8 / Stream の場合
sudo dnf install -y python3 python3-pip
```

バージョン確認:
```bash
python3 --version
```

### 1-2. Bot専用ユーザーの作成(任意・推奨)

rootで常駐/定期実行させないため、専用ユーザーを作成しておくと安全です。

```bash
sudo useradd -m -s /bin/bash rssbot
sudo su - rssbot
```

### 1-3. ファイルの配置

このディレクトリ一式を任意の場所(例: `/home/rssbot/rss-discord-bot`)に配置します。
`scp`やGit等で転送してください。

```bash
mkdir -p ~/rss-discord-bot
cd ~/rss-discord-bot
# ここに rss_discord_bot.py, config.yaml.example, requirements.txt を配置
```

### 1-4. 仮想環境の作成と依存パッケージのインストール

```bash
cd ~/rss-discord-bot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 1-5. 設定ファイルの作成

```bash
cp config.yaml.example config.yaml
vi config.yaml
```

最低限、以下を設定してください。
- `webhook_url`: DiscordのWebhook URL(サーバー設定 > 連携サービス > ウェブフック で発行)
- `feeds`: 監視したいRSS/AtomフィードのURL一覧

### 1-6. 動作確認

```bash
source venv/bin/activate
python3 rss_discord_bot.py --config config.yaml --verbose
```

初回実行時はデフォルトで全記事が「新着」としてDiscordに一気に通知されます(既存記事がDBに無いため)。
これを避けたい場合は「初回スキップ」機能が使えます(詳細は次項)。

---

## 2. フィードごとにWebhook投稿先を変える

サイトAの記事はチャンネルa、サイトBの記事はチャンネルbに投稿する、といった振り分けができます。
`feeds:` の各項目に `webhook_url` を指定すると、そのフィードの記事だけそのWebhookに投稿されます。
指定しなかったフィードは、config.yaml直下の共通デフォルト `webhook_url` が使われます。

```yaml
webhook_url: "https://discord.com/api/webhooks/.../共通デフォルト用"

feeds:
  - name: "サイトA"
    url: "https://example.com/feed.xml"
    webhook_url: "https://discord.com/api/webhooks/.../a用チャンネル"
  - name: "サイトB"
    url: "https://example.org/rss"
    webhook_url: "https://discord.com/api/webhooks/.../b用チャンネル"
  - name: "サイトC"
    url: "https://example.net/atom.xml"
    # webhook_urlを省略 -> 共通デフォルトのWebhookに投稿される
```

全フィードに個別の`webhook_url`を指定するのであれば、直下の共通デフォルト`webhook_url`自体を
省略しても構いません。ただし、いずれの`webhook_url`も設定されていないフィードがあると
起動時にエラーで停止します(設定漏れに気付けるようにするためです)。

---

## 3. RSS取得失敗時の通知(記事投稿先とは別Webhook)

サイトにアクセスできない、RSSの形式が壊れている等でフィードの取得・解析に失敗した場合、
その旨を記事投稿用とは別のWebhookに通知できます。`error_webhook_url` を設定してください。

```yaml
error_webhook_url: "https://discord.com/api/webhooks/.../エラー通知専用チャンネル"
```

- `error_webhook_url` は全フィード共通で1つだけ設定する想定です(運用監視用チャンネル等)。
- 未設定の場合、取得失敗時はDiscordには通知されず、ログファイルにのみ記録されます。
- 1つのフィードの取得に失敗しても、他のフィードの処理・通知は継続されます。
- エラー通知には フィード名・URL・エラー内容(タイムアウト、HTTPエラー、解析エラー等)が含まれます。

---

## 4. 初回実行時の通知スキップ

新しくフィードを追加した際、過去記事が大量にDiscordへ一気に通知されるのを避けたい場合は
「初回スキップ」機能が使えます。DBにそのフィードの記録が1件も無い状態(=初回実行)の場合、
現在フィードにある記事をすべて「既読」としてDBに記録するだけで、Discordへの通知は行いません。
2回目以降の実行からは通常どおり新着・更新を検知して通知します。

設定方法は2通りあります(併用可・フィード個別設定が優先されます)。

**a) config.yamlで全フィード共通にする場合**

```yaml
skip_initial_notify: true
```

**b) 特定のフィードだけ初回スキップしたい場合**

```yaml
feeds:
  - name: "サイトAのお知らせ"
    url: "https://example.com/feed.xml"
    skip_initial_notify: true
```

**c) 実行時にコマンドラインオプションで指定する場合**(config.yamlの設定より優先されます)

```bash
python3 rss_discord_bot.py --config config.yaml --skip-initial-notify
```

すでに何度か実行してしまい大量通知されてしまった後でも、対象フィードのレコードを
DBから削除すればそのフィードだけ「初回扱い」に戻せます(下記4項参照)。

---

## 5. 定期実行の設定(cron)

さくらのVPSはsystemdが使えるCentOSなので、cronでもsystemdタイマーでも構いませんが、
シンプルなcronでの設定例を示します。

```bash
crontab -e
```

以下を追記(例: 10分おきに実行):

```
*/10 * * * * cd /home/rssbot/rss-discord-bot && /home/rssbot/rss-discord-bot/venv/bin/python3 rss_discord_bot.py --config /home/rssbot/rss-discord-bot/config.yaml >> /home/rssbot/rss-discord-bot/cron.log 2>&1
```

ポイント:
- `cd`で作業ディレクトリに移動してから実行することで、相対パスの`config.yaml`やDB/ログの
  出力先がズレないようにしています(絶対パスをconfig.yaml側で指定しても構いません)。
- 仮想環境の`venv/bin/python3`を直接指定しているため、cron実行時にvenvを`activate`する必要はありません。
- ロックファイルにより、前回の実行が長引いていても多重実行にはなりません。

### 実行間隔の目安

フィード提供元に負荷をかけすぎないよう、5〜15分程度の間隔を推奨します。
更新頻度が低いサイトであれば30分〜1時間おきでも十分です。

---

## 6. ログの確認

```bash
tail -f ~/rss-discord-bot/logs/rss_discord_bot.log
```

エラーが出た場合はこのログに理由(フィード取得失敗、Discord投稿失敗など)が記録されます。
1つのフィードの取得に失敗しても他のフィードの処理は継続されます。

---

## 7. 更新検知の仕組み

- 各記事は `feed_url + guid(記事のID)` の組み合わせで一意に管理されます。
- 記事のタイトル・概要・リンクからハッシュ値を計算し、DBに保存された値と異なれば
  「更新」としてDiscordに再通知します(Embedの色とフッターで新着/更新を区別)。
- 完全に同一内容であれば再通知はされません。

## 8. アンインストール/リセット

- 通知履歴を全てリセットしたい場合は `data/rss_discord_bot.db` を削除してください
  (次回実行時に全記事が新着として再通知されます。初回スキップ設定をしていればスキップされます)。
- 特定の1フィードだけリセット(=次回を「初回扱い」に戻す)したい場合は、
  `sqlite3`コマンドでそのフィードのレコードだけ削除できます。

  ```bash
  sqlite3 data/rss_discord_bot.db \
    "DELETE FROM articles WHERE feed_url = 'https://example.com/feed.xml';"
  ```

- cronの設定行を`crontab -e`で削除すれば定期実行は止まります。
