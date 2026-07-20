#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS -> Discord Webhook 通知Bot

- 設定ファイル(config.yaml)に列挙したRSS/AtomフィードをGETし、
  新着記事・更新された既存記事を検出してDiscordにWebhook投稿する。
- 配信済み記事はSQLiteデータベースに記録し、内容のハッシュ値を保持することで
  「新着」と「更新」を区別する。
- cron等から定期実行される前提の「1回実行して終了する」バッチスクリプト。
  多重起動防止のためロックファイルを使用する。

使い方:
    python3 rss_discord_bot.py --config /path/to/config.yaml

依存パッケージ: feedparser, requests, PyYAML (requirements.txt参照)
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import logging.handlers
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import feedparser
import requests
import yaml

# --------------------------------------------------------------------------
# ロギング設定
# --------------------------------------------------------------------------

logger = logging.getLogger("rss_discord_bot")


def setup_logging(log_path: Optional[str], verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)


# --------------------------------------------------------------------------
# データ構造
# --------------------------------------------------------------------------


@dataclass
class Article:
    feed_url: str
    guid: str
    title: str
    link: str
    summary: str
    published: str  # ISO文字列 or 空文字
    content_hash: str


# --------------------------------------------------------------------------
# データベース
# --------------------------------------------------------------------------


class ArticleStore:
    """配信済み記事を記録するSQLiteストア"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS articles (
                feed_url     TEXT NOT NULL,
                guid         TEXT NOT NULL,
                title        TEXT,
                link         TEXT,
                content_hash TEXT NOT NULL,
                published    TEXT,
                first_seen   TEXT NOT NULL,
                last_notified TEXT NOT NULL,
                PRIMARY KEY (feed_url, guid)
            )
            """
        )
        self.conn.commit()

    def has_any(self, feed_url: str) -> bool:
        """指定フィードの記事が1件でもDBに記録済みかどうか。
        Falseなら「このフィードはまだ一度も処理されていない=初回実行」とみなせる。
        """
        cur = self.conn.execute(
            "SELECT 1 FROM articles WHERE feed_url = ? LIMIT 1", (feed_url,)
        )
        return cur.fetchone() is not None

    def get(self, feed_url: str, guid: str) -> Optional[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute(
            "SELECT * FROM articles WHERE feed_url = ? AND guid = ?",
            (feed_url, guid),
        )
        return cur.fetchone()

    def upsert(self, article: Article, is_new: bool) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if is_new:
            self.conn.execute(
                """
                INSERT INTO articles
                    (feed_url, guid, title, link, content_hash, published, first_seen, last_notified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article.feed_url,
                    article.guid,
                    article.title,
                    article.link,
                    article.content_hash,
                    article.published,
                    now,
                    now,
                ),
            )
        else:
            self.conn.execute(
                """
                UPDATE articles
                SET title = ?, link = ?, content_hash = ?, published = ?, last_notified = ?
                WHERE feed_url = ? AND guid = ?
                """,
                (
                    article.title,
                    article.link,
                    article.content_hash,
                    article.published,
                    now,
                    article.feed_url,
                    article.guid,
                ),
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# --------------------------------------------------------------------------
# フィード取得・解析
# --------------------------------------------------------------------------


def compute_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def parse_feed(feed_url: str, user_agent: str, timeout: int = 20) -> list[Article]:
    """フィードを取得しArticleのリストを返す。取得失敗時は例外を投げる。"""
    resp = requests.get(feed_url, headers={"User-Agent": user_agent}, timeout=timeout)
    resp.raise_for_status()

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        # 構文エラーがあり、かつ記事も1件も取れていない場合のみ異常とみなす
        raise ValueError(f"フィード解析に失敗しました: {parsed.bozo_exception}")

    articles: list[Article] = []
    for entry in parsed.entries:
        guid = entry.get("id") or entry.get("guid") or entry.get("link")
        if not guid:
            # GUIDが全く取れない記事はスキップ(記録のしようがないため)
            logger.warning("GUIDを取得できない記事をスキップします: %s", entry.get("title"))
            continue

        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        summary = entry.get("summary", entry.get("description", "")).strip()

        published_struct = entry.get("updated_parsed") or entry.get("published_parsed")
        if published_struct:
            published = datetime(*published_struct[:6], tzinfo=timezone.utc).isoformat()
        else:
            published = ""

        content_hash = compute_hash(title, summary, link)

        articles.append(
            Article(
                feed_url=feed_url,
                guid=str(guid),
                title=title,
                link=link,
                summary=summary,
                published=published,
                content_hash=content_hash,
            )
        )
    return articles


# --------------------------------------------------------------------------
# Discord通知
# --------------------------------------------------------------------------


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def send_discord_notification(
    webhook_url: str,
    feed_name: str,
    article: Article,
    is_update: bool,
    max_retries: int = 3,
) -> None:
    label = "記事が更新されました" if is_update else "新着記事"
    color = 0xF1C40F if is_update else 0x3498DB  # 更新=黄, 新着=青

    embed = {
        "title": truncate(article.title or "(タイトルなし)", 256),
        "url": article.link or None,
        "description": truncate(strip_html(article.summary), 500),
        "color": color,
        "footer": {"text": f"{feed_name} ・ {label}"},
        "timestamp": article.published or datetime.now(timezone.utc).isoformat(),
    }
    payload = {"embeds": [embed]}

    for attempt in range(1, max_retries + 1):
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if resp.status_code in (200, 204):
            return
        if resp.status_code == 429:
            # レート制限。指定された秒数待って再試行
            retry_after = 1.0
            try:
                retry_after = float(resp.json().get("retry_after", 1.0))
            except Exception:
                pass
            logger.warning("Discordのレート制限に到達。%.1f秒待機します。", retry_after)
            time.sleep(retry_after + 0.5)
            continue
        # その他のエラー
        logger.error(
            "Discord投稿に失敗 (status=%s, attempt=%d): %s",
            resp.status_code,
            attempt,
            resp.text[:300],
        )
        time.sleep(2 * attempt)
    raise RuntimeError(f"Discord投稿がリトライ上限に達しました: {article.title}")


def send_error_notification(
    error_webhook_url: str,
    feed_name: str,
    feed_url: str,
    error_message: str,
    max_retries: int = 3,
) -> None:
    """RSS取得・解析に失敗したことを、記事通知用とは別のWebhookに知らせる。"""
    embed = {
        "title": "RSSフィードの取得に失敗しました",
        "color": 0xE74C3C,  # 赤
        "fields": [
            {"name": "フィード名", "value": feed_name or "(不明)", "inline": True},
            {"name": "URL", "value": feed_url or "(不明)", "inline": False},
            {"name": "エラー内容", "value": truncate(error_message, 1000), "inline": False},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = {"embeds": [embed]}

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(error_webhook_url, json=payload, timeout=15)
        except requests.RequestException as e:
            logger.error("エラー通知の送信中に例外が発生しました (attempt=%d): %s", attempt, e)
            time.sleep(2 * attempt)
            continue

        if resp.status_code in (200, 204):
            return
        if resp.status_code == 429:
            retry_after = 1.0
            try:
                retry_after = float(resp.json().get("retry_after", 1.0))
            except Exception:
                pass
            time.sleep(retry_after + 0.5)
            continue
        logger.error(
            "エラー通知の送信に失敗 (status=%s, attempt=%d): %s",
            resp.status_code,
            attempt,
            resp.text[:300],
        )
        time.sleep(2 * attempt)

    logger.error("エラー通知のリトライ上限に達しました [%s]", feed_name)


def strip_html(text: str) -> str:
    """簡易的にHTMLタグを除去する(概要文表示用)。"""
    import re

    text = re.sub(r"<[^>]+>", "", text or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return text.strip()


# --------------------------------------------------------------------------
# メイン処理
# --------------------------------------------------------------------------


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not config or "feeds" not in config:
        raise ValueError("config.yaml に feeds を設定してください。")

    default_webhook = config.get("webhook_url")
    missing = []
    for feed_conf in config["feeds"]:
        if not feed_conf.get("webhook_url") and not default_webhook:
            missing.append(feed_conf.get("name", feed_conf.get("url", "(不明なフィード)")))
    if missing:
        raise ValueError(
            "以下のフィードに通知先Webhookがありません。"
            "各フィードに webhook_url を設定するか、config.yaml直下に"
            f"webhook_url(共通のデフォルト)を設定してください: {', '.join(missing)}"
        )
    return config


def resolve_feed_webhook_url(feed_conf: dict, config: dict) -> str:
    """フィード個別のwebhook_url > config直下のwebhook_url(デフォルト) の優先順で解決する。"""
    return feed_conf.get("webhook_url") or config.get("webhook_url")


def acquire_lock(lock_path: str):
    """多重起動防止用ロック。取得できなければNoneを返す。"""
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)) or ".", exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def process_feed(
    store: ArticleStore,
    config: dict,
    feed_conf: dict,
    user_agent: str,
    error_webhook_url: Optional[str],
    default_skip_initial: bool = False,
) -> None:
    feed_url = feed_conf["url"]
    feed_name = feed_conf.get("name", feed_url)
    webhook_url = resolve_feed_webhook_url(feed_conf, config)

    try:
        articles = parse_feed(feed_url, user_agent)
    except Exception as e:
        logger.error("フィード取得に失敗しました [%s]: %s", feed_name, e)
        if error_webhook_url:
            try:
                send_error_notification(error_webhook_url, feed_name, feed_url, str(e))
            except Exception as notify_err:
                logger.error("エラー通知の送信自体に失敗しました [%s]: %s", feed_name, notify_err)
        return

    logger.info("フィード取得完了 [%s]: %d件", feed_name, len(articles))

    # このフィードがまだ一度もDBに記録されていない = 初回実行かどうかを判定
    is_first_run_for_feed = not store.has_any(feed_url)
    # フィード個別設定 > 全体設定 の優先順で「初回は通知しない」を決定
    skip_initial = feed_conf.get("skip_initial_notify", default_skip_initial)

    if is_first_run_for_feed and skip_initial:
        logger.info(
            "初回実行のため通知をスキップし、%d件を既読として記録します [%s]",
            len(articles),
            feed_name,
        )
        for article in articles:
            store.upsert(article, is_new=True)
        return

    for article in articles:
        existing = store.get(feed_url, article.guid)

        if existing is None:
            # 新着記事
            try:
                send_discord_notification(webhook_url, feed_name, article, is_update=False)
                store.upsert(article, is_new=True)
                logger.info("新着記事を通知しました: %s", article.title)
            except Exception as e:
                logger.error("新着記事の通知に失敗しました: %s (%s)", article.title, e)

        elif existing["content_hash"] != article.content_hash:
            # 更新記事
            try:
                send_discord_notification(webhook_url, feed_name, article, is_update=True)
                store.upsert(article, is_new=False)
                logger.info("更新記事を通知しました: %s", article.title)
            except Exception as e:
                logger.error("更新記事の通知に失敗しました: %s (%s)", article.title, e)

        # 変化なしの場合は何もしない


def main() -> int:
    parser = argparse.ArgumentParser(description="RSSフィードをDiscordに通知するBot")
    parser.add_argument(
        "--config", default="config.yaml", help="設定ファイルのパス (デフォルト: config.yaml)"
    )
    parser.add_argument("--verbose", action="store_true", help="デバッグログを出力する")
    parser.add_argument(
        "--skip-initial-notify",
        action="store_true",
        default=None,
        help="指定すると、まだ一度もDBに記録のないフィードは初回実行時に通知せず既読登録のみ行う"
        "(config.yamlのskip_initial_notify設定より優先されます)",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"設定ファイルの読み込みに失敗しました: {e}", file=sys.stderr)
        return 1

    setup_logging(config.get("log_path", "./logs/rss_discord_bot.log"), args.verbose)

    lock_path = config.get("lock_path", "./rss_discord_bot.lock")
    lock_file = acquire_lock(lock_path)
    if lock_file is None:
        logger.warning("既に実行中のプロセスが存在するため終了します。")
        return 0

    try:
        db_path = config.get("db_path", "./rss_discord_bot.db")
        store = ArticleStore(db_path)
        error_webhook_url = config.get("error_webhook_url")  # 取得失敗通知専用(任意)
        user_agent = config.get("user_agent", "RSSDiscordBot/1.0")

        # CLI引数(--skip-initial-notify)が指定されていればconfig.yamlより優先する
        default_skip_initial = config.get("skip_initial_notify", False)
        if args.skip_initial_notify is not None:
            default_skip_initial = args.skip_initial_notify

        for feed_conf in config["feeds"]:
            process_feed(
                store,
                config,
                feed_conf,
                user_agent,
                error_webhook_url,
                default_skip_initial,
            )

        store.close()
        logger.info("全フィードの処理が完了しました。")
        return 0
    except Exception as e:
        logger.exception("予期しないエラーが発生しました: %s", e)
        return 1
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    sys.exit(main())
