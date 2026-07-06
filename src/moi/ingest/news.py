"""News headline ingester (RSS, stdlib XML parsing — no extra dependencies).

Per-ticker headlines via Yahoo Finance RSS plus configurable sector feeds
(``config/news.yaml``). Items are deduplicated by URL hash. LLM scoring happens in
Phase 3; this module only ingests raw headlines.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import duckdb
import httpx
import yaml

from moi.config import CONFIG_DIR
from moi.logging import get_logger
from moi.runlog import track_run
from moi.universe import candidate_tickers

log = get_logger(__name__)


@dataclass(frozen=True)
class NewsItem:
    id: str
    ticker: str | None
    title: str
    url: str
    published_at: datetime | None
    feed: str
    summary: str | None


def parse_rss(xml_text: str, *, feed: str, ticker: str | None = None) -> list[NewsItem]:
    """Parse RSS 2.0 (and tolerate Atom) into NewsItems. Malformed XML yields []."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        log.warning("rss_parse_error", feed=feed)
        return []

    items: list[NewsItem] = []
    # RSS 2.0: channel/item; Atom: {ns}entry
    entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for entry in entries:
        def find_text(*tags: str, entry: ET.Element = entry) -> str | None:
            for tag in tags:
                el = entry.find(tag)
                if el is not None:
                    if el.text and el.text.strip():
                        return el.text.strip()
                    href = el.get("href")  # Atom <link href=...>
                    if href:
                        return href
            return None

        title = find_text("title", "{http://www.w3.org/2005/Atom}title")
        url = find_text("link", "{http://www.w3.org/2005/Atom}link")
        if not title or not url:
            continue
        pub_raw = find_text(
            "pubDate", "{http://www.w3.org/2005/Atom}published",
            "{http://www.w3.org/2005/Atom}updated",
        )
        published: datetime | None = None
        if pub_raw:
            try:
                published = parsedate_to_datetime(pub_raw)
            except (TypeError, ValueError):
                try:
                    published = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
                except ValueError:
                    published = None
        summary = find_text("description", "{http://www.w3.org/2005/Atom}summary")
        items.append(
            NewsItem(
                id=hashlib.sha1(url.encode()).hexdigest()[:16],
                ticker=ticker,
                title=title,
                url=url,
                published_at=published,
                feed=feed,
                summary=(summary or "")[:1000] or None,
            )
        )
    return items


def upsert_news(con: duckdb.DuckDBPyConnection, items: list[NewsItem]) -> int:
    if not items:
        return 0
    con.executemany(
        """
        INSERT INTO news_items (id, ticker, title, url, published_at, feed, summary)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO NOTHING
        """,
        [
            (i.id, i.ticker, i.title, i.url, i.published_at, i.feed, i.summary)
            for i in items
        ],
    )
    return len(items)


def collect_news(con: duckdb.DuckDBPyConnection, config_path: Path | None = None) -> int:
    """Fetch all configured feeds. Each feed is best-effort; failures log and continue."""
    cfg = yaml.safe_load((config_path or CONFIG_DIR / "news.yaml").read_text()) or {}
    total = 0
    with track_run(con, job="collect.news") as run:
        # Some feeds (Yahoo) return empty results for non-browser user agents.
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh) moi-news/0.1"}
        with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
            per_ticker = cfg.get("per_ticker", {})
            if per_ticker.get("enabled"):
                template = per_ticker["url_template"]
                for ticker in candidate_tickers():
                    try:
                        resp = client.get(template.format(ticker=ticker))
                        resp.raise_for_status()
                        items = parse_rss(resp.text, feed="yahoo", ticker=ticker)
                        total += upsert_news(con, items)
                    except Exception as exc:
                        log.warning("news_ticker_failed", ticker=ticker, error=str(exc))
            for feed in cfg.get("sector_feeds", []) or []:
                try:
                    resp = client.get(feed["url"])
                    resp.raise_for_status()
                    items = parse_rss(resp.text, feed=feed["name"])
                    total += upsert_news(con, items)
                except Exception as exc:
                    log.warning("news_feed_failed", feed=feed.get("name"), error=str(exc))
        run.add_rows(total)
    log.info("news_done", items=total)
    return total
