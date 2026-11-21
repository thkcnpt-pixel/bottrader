"""Multi-source Vietnamese financial news aggregator & HTML cleaner.

Combines:
  - vnstock company news & corporate events/dividends
  - RSS feeds: CafeF, Vietstock, VnEconomy
  - Google News RSS filtered by ticker

Token optimization:
  - Strips HTML tags, scripts, ads, navigation
  - Deduplicates and keeps 5-8 newest articles
  - Truncates each article to <= 900 words (~800-1,000 words target)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

RSS_FEEDS = (
    ("CafeF", "https://cafef.vn/thi-truong-chung-khoan.rss"),
    ("Vietstock", "https://vietstock.vn/830/chung-khoan/co-phieu.rss"),
    ("VnEconomy", "https://vneconomy.vn/chung-khoan.rss"),
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

MAX_ARTICLES = 8
MAX_WORDS_PER_ARTICLE = 900
REQUEST_TIMEOUT = 8


def clean_html_to_text(raw_html: str) -> str:
    """Strip scripts, styles, ads, and excess whitespace from HTML."""
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "header", "footer", "nav", "aside", "form"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def truncate_words(text: str, max_words: int = MAX_WORDS_PER_ARTICLE) -> str:
    """Truncate text to at most max_words words."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "..."


def _fetch_article_body(url: str) -> str:
    """Best-effort fetch and clean main body text of a news article."""
    if not url or not url.startswith("http"):
        return ""
    # Skip Google News redirect URLs that often require JS redirects
    if "news.google.com/rss/articles" in url:
        return ""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200 or not resp.text:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe", "header", "footer", "nav", "aside", "form"]):
            tag.decompose()

        # Prefer <article> or common Vietnamese financial news body containers
        container = (
            soup.find("article")
            or soup.find(class_=re.compile(r"(detail-content|contentdetail|article-body|main-content)", re.I))
            or soup.body
        )
        if not container:
            return ""
        paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
        body = " ".join(p for p in paragraphs if len(p) > 25)
        if not body:
            body = container.get_text(" ", strip=True)
        body = re.sub(r"\s+", " ", body).strip()
        return truncate_words(body, MAX_WORDS_PER_ARTICLE)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not fetch article body (%s): %s", url, exc)
        return ""


def _mentions_ticker(text: str, ticker: str) -> bool:
    """Check if text mentions ticker as a standalone word."""
    if not text:
        return False
    pattern = rf"(?<![A-Z0-9]){re.escape(ticker.upper())}(?![A-Z0-9])"
    return bool(re.search(pattern, text.upper()))


def _fetch_rss_entries(url: str) -> List[Any]:
    """Fetch and parse RSS feed safely with timeout."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return []
        parsed = feedparser.parse(resp.content)
        return list(parsed.entries or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("RSS fetch failed for %s: %s", url, exc)
        return []


def collect_ticker_news(
    ticker: str,
    vnstock_news: List[Dict[str, Any]] | None = None,
    vnstock_events: List[Dict[str, Any]] | None = None,
    max_articles: int = MAX_ARTICLES,
) -> Dict[str, Any]:
    """Aggregate, clean, deduplicate, and truncate news + dividend events for a ticker."""
    ticker = ticker.upper().strip()
    articles: List[Dict[str, str]] = []
    seen_titles = set()

    def _add_article(title: str, source: str, date: str, url: str, summary: str) -> None:
        clean_title = clean_html_to_text(title)
        if not clean_title:
            return
        norm_key = re.sub(r"\W+", "", clean_title.lower())[:60]
        if norm_key in seen_titles:
            return
        seen_titles.add(norm_key)
        clean_summary = truncate_words(clean_html_to_text(summary), MAX_WORDS_PER_ARTICLE)
        articles.append({
            "title": clean_title,
            "source": source,
            "date": date,
            "url": url,
            "content": clean_summary,
        })

    # 1. vnstock company news (highest relevance)
    for item in (vnstock_news or []):
        _add_article(
            title=str(item.get("title", "")),
            source=str(item.get("source", "vnstock")),
            date=str(item.get("date", "")),
            url=str(item.get("url", "")),
            summary=str(item.get("summary", "")),
        )

    # 2. Google News RSS by ticker (Vietnamese)
    gnews_query = quote_plus(f'"{ticker}" cổ phiếu OR chứng khoán')
    gnews_url = f"https://news.google.com/rss/search?q={gnews_query}&hl=vi&gl=VN&ceid=VN:vi"
    for entry in _fetch_rss_entries(gnews_url)[:6]:
        title = getattr(entry, "title", "")
        summary = getattr(entry, "summary", "")
        if _mentions_ticker(f"{title} {summary}", ticker):
            _add_article(
                title=title,
                source="GoogleNews",
                date=getattr(entry, "published", ""),
                url=getattr(entry, "link", ""),
                summary=summary,
            )

    # 3. Vietnamese Financial RSS (CafeF, Vietstock, VnEconomy)
    for feed_name, feed_url in RSS_FEEDS:
        for entry in _fetch_rss_entries(feed_url)[:30]:
            title = getattr(entry, "title", "")
            summary = getattr(entry, "summary", "")
            if _mentions_ticker(f"{title} {summary}", ticker):
                _add_article(
                    title=title,
                    source=feed_name,
                    date=getattr(entry, "published", ""),
                    url=getattr(entry, "link", ""),
                    summary=summary,
                )

    # Keep top N articles and enrich short summaries by scraping clean body text
    selected = articles[:max_articles]
    for art in selected:
        if len(art["content"].split()) < 60 and art.get("url"):
            scraped = _fetch_article_body(art["url"])
            if scraped:
                art["content"] = scraped

    return {
        "ticker": ticker,
        "articles": selected,
        "events": (vnstock_events or [])[:6],
    }


NEGATIVE_NEWS_KEYWORDS = (
    "khởi tố",
    "bắt tạm giam",
    "cơ quan điều tra",
    "hủy niêm yết",
    "đình chỉ giao dịch",
    "hạn chế giao dịch",
    "cảnh cáo",
    "kiểm soát đặc biệt",
    "xử phạt",
    "truy thu thuế",
    "thua lỗ nặng",
    "lỗ kỷ lục",
    "vỡ nợ",
    "chậm trả lãi trái phiếu",
)


def detect_holding_negative_news(
    ticker: str,
    news_bundle: Optional[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Scan news articles for a holding ticker and return any matching severe risk keywords."""
    if not news_bundle:
        return []
    articles = news_bundle.get("articles") or []
    flagged: List[Dict[str, str]] = []

    for art in articles:
        title = str(art.get("title") or "").strip()
        summary = str(art.get("summary") or art.get("content") or "").strip()
        combined_lower = f"{title} {summary}".lower()
        matched_kw = [kw for kw in NEGATIVE_NEWS_KEYWORDS if kw in combined_lower]
        if matched_kw:
            flagged.append({
                "ticker": ticker,
                "title": title,
                "source": str(art.get("source") or "News"),
                "matched_keywords": ", ".join(matched_kw),
            })

    return flagged
