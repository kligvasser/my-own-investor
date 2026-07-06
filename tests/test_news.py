"""RSS parsing and news dedup."""

from __future__ import annotations

from moi.ingest.news import parse_rss, upsert_news

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Feed</title>
  <item>
    <title>Vertiv wins hyperscaler cooling order</title>
    <link>https://example.com/vrt-order</link>
    <pubDate>Mon, 06 Jul 2026 12:00:00 GMT</pubDate>
    <description>Big order.</description>
  </item>
  <item>
    <title>No link item is skipped</title>
  </item>
</channel></rss>"""


def test_parse_rss() -> None:
    items = parse_rss(RSS, feed="test", ticker="VRT")
    assert len(items) == 1
    item = items[0]
    assert item.ticker == "VRT"
    assert item.title.startswith("Vertiv")
    assert item.published_at is not None
    assert item.published_at.year == 2026


def test_parse_rss_malformed() -> None:
    assert parse_rss("<not-xml", feed="x") == []


def test_upsert_news_dedup(db) -> None:
    items = parse_rss(RSS, feed="test", ticker="VRT")
    upsert_news(db, items)
    upsert_news(db, items)  # same URL hash → single row
    assert db.execute("SELECT count(*) FROM news_items").fetchone()[0] == 1
