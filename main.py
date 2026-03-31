#!/usr/bin/env python3
"""
Bot Telegram de veille "Claude Code" — MVP
Pipeline: collect → normalize → deduplicate → score_and_filter → summarize → send_telegram
"""

import argparse
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Optional
from zoneinfo import ZoneInfo

import requests

# ─── Configuration ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

TIMEOUT = 15        # seconds — HTTP fetches
TIMEOUT_LLM = 30    # seconds — LLM inference (Gemini can be slow)

SEARCH_QUERIES = [
    "Claude Code",
    "Anthropic Claude Code",
    "Claude Code update",
    "claude code CLI",
]

SEO_NOISE_MARKERS = [
    "best ai tools",
    "top 10",
    "vs chatgpt",
    "best tools",
    "top tools",
    "ai tools list",
]

RELEVANCE_TERMS = [
    "claude code",
    "claude-code",
    "code.claude",
]

MIN_SCORE = 30
MAX_ITEMS_FOR_LLM = 15
MAX_ITEMS_IN_MESSAGE = 5

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
)


# ─── Data Model ───────────────────────────────────────────────────────────────

@dataclass
class NewsItem:
    title: str
    url: str
    source_name: str
    source_type: str        # "official" | "community" | "aggregator"
    published_at: Optional[str]  # ISO 8601 or None
    snippet: str
    query_used: str
    score: int = 0
    category: str = ""
    one_line_summary: str = ""
    # Phase 3 — IA enrichment
    importance: str = ""    # "critical" | "notable" | "minor"
    sentiment: str = ""     # "positive" | "neutral" | "negative" | "mixed"
    why_relevant: str = ""  # short explanation
    raw_score: int = 0      # source-native score (HN pts, reactions, etc.)


# ─── Collect ──────────────────────────────────────────────────────────────────

def fetch_anthropic_rss() -> list[dict]:
    """Fetch Anthropic blog — tries RSS feed first, scrapes blog page as fallback."""
    items = []

    # Try known RSS URLs
    for rss_url in [
        "https://www.anthropic.com/blog.rss",
        "https://www.anthropic.com/feed.xml",
        "https://www.anthropic.com/rss",
    ]:
        try:
            resp = requests.get(rss_url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            for item in root.findall(".//item"):
                title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                pub_date = item.findtext("pubDate", "").strip()
                description = re.sub(r"<[^>]+>", "", item.findtext("description", ""))[:300]
                items.append({
                    "title": title,
                    "url": link,
                    "source_name": "Anthropic Blog",
                    "source_type": "official",
                    "published_at": pub_date,
                    "snippet": description.strip(),
                    "query_used": "Anthropic Blog RSS",
                })
            if items:
                log.info(f"Anthropic RSS ({rss_url}): {len(items)} items")
                return items
        except Exception:
            continue

    # Fallback: scrape the blog page with Scrapling CSS selectors
    log.info("Anthropic RSS not found — scraping blog page (Scrapling)")
    try:
        resp = requests.get(
            "https://www.anthropic.com/blog",
            timeout=TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        try:
            from scrapling.parser import Selector
            page = Selector(resp.text)
            for article in page.css('a[href*="/blog/"]')[:15]:
                href = article.attrib.get("href", "")
                if not href:
                    continue
                url = f"https://www.anthropic.com{href}" if href.startswith("/") else href
                title = (
                    article.css("h2::text, h3::text").get()
                    or article.css("::text").get(default="")
                ).strip()
                if not title:
                    slug = href.rstrip("/").split("/")[-1]
                    title = slug.replace("-", " ").title()
                items.append({
                    "title": title,
                    "url": url,
                    "source_name": "Anthropic Blog",
                    "source_type": "official",
                    "published_at": None,
                    "snippet": f"Article from Anthropic Blog: {title}"[:300],
                    "query_used": "Anthropic Blog scrape",
                })
            log.info(f"Anthropic Blog scrape (Scrapling): {len(items)} items")
        except ImportError:
            # Scrapling not available — fall back to regex
            content = resp.text
            slugs_seen: set[str] = set()
            blog_urls: list[tuple[str, str]] = []
            for path in re.findall(
                r'''href=["'](?:https://www\.anthropic\.com)?(/blog/([a-z0-9][a-z0-9-]{2,}))["']''',
                content,
            ):
                full_path, name = path
                if name and name not in slugs_seen:
                    slugs_seen.add(name)
                    blog_urls.append((f"https://www.anthropic.com{full_path}", name))
            headings = [
                re.sub(r"\s+", " ", h).strip()
                for h in re.findall(r"<h[123][^>]*>([^<]{10,150})</h[123]>", content)
                if h.strip()
            ]
            for i, (url, slug) in enumerate(blog_urls[:15]):
                title = headings[i] if i < len(headings) else slug.replace("-", " ").title()
                items.append({
                    "title": title,
                    "url": url,
                    "source_name": "Anthropic Blog",
                    "source_type": "official",
                    "published_at": None,
                    "snippet": f"Article from Anthropic Blog: {title}"[:300],
                    "query_used": "Anthropic Blog scrape",
                })
            log.info(f"Anthropic Blog scrape (regex): {len(items)} items")
    except Exception as e:
        log.warning(f"Anthropic Blog scrape failed: {e}")

    return items


def fetch_anthropic_changelog() -> list[dict]:
    """Fetch Anthropic changelog page via Scrapling CSS selectors (regex fallback)."""
    url = "https://docs.anthropic.com/en/docs/changelog"
    items = []
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        try:
            from scrapling.parser import Selector
            page = Selector(resp.text)
            for heading in page.css("h2, h3")[:10]:
                title = heading.css("::text").get(default="").strip()
                if not title:
                    continue
                anchor = heading.attrib.get("id", "")
                entry_url = f"{url}#{anchor}" if anchor else url
                items.append({
                    "title": f"Anthropic Changelog: {title}",
                    "url": entry_url,
                    "source_name": "Anthropic Changelog",
                    "source_type": "official",
                    "published_at": None,
                    "snippet": f"Entry from Anthropic docs changelog: {title}"[:300],
                    "query_used": "Anthropic Changelog",
                })
            log.info(f"Anthropic Changelog (Scrapling): {len(items)} items")
        except ImportError:
            content = resp.text
            headings = re.findall(r"<h[23][^>]*>([^<]+)</h[23]>", content)
            for heading in headings[:10]:
                heading = heading.strip()
                if not heading:
                    continue
                slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
                entry_url = f"{url}#{slug}" if slug else url
                items.append({
                    "title": f"Anthropic Changelog: {heading}",
                    "url": entry_url,
                    "source_name": "Anthropic Changelog",
                    "source_type": "official",
                    "published_at": None,
                    "snippet": f"Entry from Anthropic docs changelog: {heading}"[:300],
                    "query_used": "Anthropic Changelog",
                })
            log.info(f"Anthropic Changelog (regex): {len(items)} items")
    except Exception as e:
        log.warning(f"Anthropic Changelog failed: {e}")
    return items


def fetch_hackernews(query: str) -> list[dict]:
    """Fetch recent HN stories via Algolia API."""
    url = "https://hn.algolia.com/api/v1/search_by_date"
    items = []
    try:
        cutoff = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp())
        params = {
            "query": query,
            "tags": "story",
            "numericFilters": f"created_at_i>{cutoff}",
        }
        resp = requests.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        for hit in data.get("hits", []):
            title = hit.get("title", "").strip()
            story_url = hit.get("url") or (
                f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
            )
            points = hit.get("points", 0) or 0
            created_at = hit.get("created_at", "")
            items.append({
                "title": title,
                "url": story_url,
                "source_name": "Hacker News",
                "source_type": "community",
                "published_at": created_at,
                "snippet": f"HN ({points} pts) — {title}"[:300],
                "query_used": query,
                "raw_score": points,
            })
        log.info(f"HN '{query}': {len(items)} items")
    except Exception as e:
        log.warning(f"HN fetch failed for '{query}': {e}")
    return items


def get_reddit_token(client_id: str, client_secret: str) -> Optional[str]:
    """Obtain a Reddit OAuth2 bearer token via client_credentials grant."""
    try:
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            headers={"User-Agent": "python:claude-code-veille:v2.0"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if token:
            log.info("Reddit OAuth token obtained successfully")
        return token
    except Exception as e:
        log.warning(f"Reddit OAuth token request failed: {e}")
        return None


def fetch_reddit(query: str, bearer_token: Optional[str] = None) -> list[dict]:
    """Fetch recent Reddit posts. Tries OAuth first, then falls back to public endpoints."""
    items = []
    params = {"q": query, "sort": "new", "t": "day", "limit": 25}

    def _parse_posts(data: dict) -> list[dict]:
        result = []
        for post in data.get("data", {}).get("children", []):
            p = post.get("data", {})
            title = p.get("title", "").strip()
            permalink = f"https://reddit.com{p.get('permalink', '')}"
            selftext = p.get("selftext", "")[:200]
            created_utc = p.get("created_utc")
            pub_date = (
                datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat()
                if created_utc else None
            )
            subreddit = p.get("subreddit_name_prefixed", "")
            score = p.get("score", 0) or 0
            result.append({
                "title": title,
                "url": permalink,
                "source_name": "Reddit",
                "source_type": "community",
                "published_at": pub_date,
                "snippet": f"{subreddit} — {selftext or title}"[:300],
                "query_used": query,
                "raw_score": score,
            })
        return result

    # Try OAuth path first
    if bearer_token:
        try:
            resp = requests.get(
                "https://oauth.reddit.com/search",
                params=params,
                timeout=TIMEOUT,
                headers={
                    "Authorization": f"Bearer {bearer_token}",
                    "User-Agent": "python:claude-code-veille:v2.0",
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            items = _parse_posts(resp.json())
            log.info(f"Reddit '{query}': {len(items)} items")
            return items
        except Exception as e:
            log.warning(f"Reddit OAuth failed for '{query}': {e} — falling back to public API")

    # Public API fallback (also used when no token)
    public_headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    for base in ["https://www.reddit.com/search.json", "https://old.reddit.com/search.json"]:
        try:
            resp = requests.get(base, params=params, timeout=TIMEOUT, headers=public_headers)
            resp.raise_for_status()
            items = _parse_posts(resp.json())
            log.info(f"Reddit '{query}': {len(items)} items")
            return items
        except Exception as e:
            log.warning(f"Reddit fetch failed ({base}) for '{query}': {e}")

    return items


def fetch_google_news(query: str) -> list[dict]:
    """Fetch from Google News RSS."""
    url = "https://news.google.com/rss/search"
    items = []
    try:
        params = {"q": query, "hl": "en", "gl": "US", "ceid": "US:en"}
        resp = requests.get(
            url, params=params, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub_date = item.findtext("pubDate", "").strip()
            description = re.sub(
                r"<[^>]+>", "", item.findtext("description", "")
            )[:300]
            items.append({
                "title": title,
                "url": link,
                "source_name": "Google News",
                "source_type": "aggregator",
                "published_at": pub_date,
                "snippet": description.strip() or title,
                "query_used": query,
            })
        log.info(f"Google News '{query}': {len(items)} items")
    except Exception as e:
        log.warning(f"Google News fetch failed for '{query}': {e}")
    return items


def fetch_github_releases() -> list[dict]:
    """Fetch releases from the anthropics/claude-code GitHub repository."""
    url = "https://api.github.com/repos/anthropics/claude-code/releases"
    items = []
    try:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "ClaudeCodeVeille/2.0"}
        github_token = os.environ.get("CACHE_GITHUB_TOKEN")
        if github_token:
            headers["Authorization"] = f"token {github_token}"
        resp = requests.get(url, params={"per_page": 10}, timeout=TIMEOUT, headers=headers)
        resp.raise_for_status()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        for release in resp.json():
            published_at = release.get("published_at", "")
            dt = parse_date(published_at)
            if dt and dt < cutoff:
                continue
            title = release.get("name") or release.get("tag_name", "")
            body = re.sub(r"<[^>]+>", "", release.get("body", ""))[:300]
            items.append({
                "title": f"Claude Code Release: {title}",
                "url": release.get("html_url", ""),
                "source_name": "GitHub Releases",
                "source_type": "official",
                "published_at": published_at,
                "snippet": body or f"New release: {title}",
                "query_used": "GitHub Releases API",
            })
        log.info(f"GitHub Releases: {len(items)} items")
    except Exception as e:
        log.warning(f"GitHub Releases fetch failed: {e}")
    return items


# ─── Phase 1 — Nouvelles sources ──────────────────────────────────────────────

def fetch_devto(query: str = "claude code") -> list[dict]:
    """Fetch Dev.to articles via public API (no auth required)."""
    items = []
    seen_ids: set[int] = set()
    base_url = "https://dev.to/api/articles"
    headers = {"User-Agent": "ClaudeCodeVeille/2.0"}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

    def _parse_articles(articles: list) -> list[dict]:
        result = []
        for art in articles:
            art_id = art.get("id")
            if not art_id or art_id in seen_ids:
                continue
            published = art.get("published_at") or art.get("created_at", "")
            dt = parse_date(published)
            if dt and dt < cutoff:
                continue
            seen_ids.add(art_id)
            title = art.get("title", "").strip()
            url = art.get("url", "").strip()
            description = art.get("description", "").strip()
            reactions = art.get("positive_reactions_count", 0) or 0
            result.append({
                "title": title,
                "url": url,
                "source_name": "Dev.to",
                "source_type": "community",
                "published_at": published,
                "snippet": description[:300] or f"Dev.to article: {title}",
                "query_used": query,
                "raw_score": reactions,
            })
        return result

    # Search by tag
    for tag in ["claudecode", "anthropic"]:
        try:
            resp = requests.get(
                base_url, params={"tag": tag, "per_page": 20}, timeout=TIMEOUT, headers=headers
            )
            resp.raise_for_status()
            items.extend(_parse_articles(resp.json()))
        except Exception as e:
            log.warning(f"Dev.to tag '{tag}' failed: {e}")

    # Search by query
    try:
        resp = requests.get(
            "https://dev.to/search/feed_content",
            params={"search_fields": "title", "search": query, "per_page": 20},
            timeout=TIMEOUT,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        articles = data if isinstance(data, list) else data.get("result", [])
        items.extend(_parse_articles(articles))
    except Exception as e:
        log.warning(f"Dev.to search '{query}' failed: {e}")

    log.info(f"Dev.to: {len(items)} items")
    return items


def fetch_stackoverflow(query: str = "claude code") -> list[dict]:
    """Fetch recent Stack Overflow questions (no API key required for 300 req/day)."""
    items = []
    try:
        cutoff_ts = int((datetime.now(timezone.utc) - timedelta(hours=48)).timestamp())
        params = {
            "q": query,
            "site": "stackoverflow",
            "sort": "creation",
            "order": "desc",
            "pagesize": 10,
            "fromdate": cutoff_ts,
        }
        resp = requests.get(
            "https://api.stackexchange.com/2.3/search/advanced",
            params=params,
            timeout=TIMEOUT,
            headers={"User-Agent": "ClaudeCodeVeille/2.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        for q in data.get("items", []):
            title = q.get("title", "").strip()
            link = q.get("link", "").strip()
            creation_date = q.get("creation_date")
            pub_date = (
                datetime.fromtimestamp(creation_date, tz=timezone.utc).isoformat()
                if creation_date else None
            )
            score = q.get("score", 0) or 0
            answer_count = q.get("answer_count", 0) or 0
            items.append({
                "title": title,
                "url": link,
                "source_name": "Stack Overflow",
                "source_type": "community",
                "published_at": pub_date,
                "snippet": f"Stack Overflow ({answer_count} answers, score {score}) — {title}"[:300],
                "query_used": query,
                "raw_score": score,
            })
        log.info(f"Stack Overflow '{query}': {len(items)} items")
    except Exception as e:
        log.warning(f"Stack Overflow fetch failed: {e}")
    return items


def fetch_github_discussions() -> list[dict]:
    """Fetch recent GitHub Issues/Discussions from anthropics/claude-code."""
    items = []
    try:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "ClaudeCodeVeille/2.0"}
        github_token = os.environ.get("CACHE_GITHUB_TOKEN")
        if github_token:
            headers["Authorization"] = f"token {github_token}"
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        cutoff_iso = cutoff.isoformat()
        resp = requests.get(
            "https://api.github.com/repos/anthropics/claude-code/issues",
            params={"state": "open", "sort": "created", "per_page": 15, "since": cutoff_iso},
            timeout=TIMEOUT,
            headers=headers,
        )
        resp.raise_for_status()
        for issue in resp.json():
            # Skip pull requests (they appear in issues endpoint too)
            if issue.get("pull_request"):
                continue
            title = issue.get("title", "").strip()
            url = issue.get("html_url", "").strip()
            created_at = issue.get("created_at", "")
            body = (issue.get("body") or "")[:200]
            comments = issue.get("comments", 0) or 0
            labels = [lb.get("name", "") for lb in issue.get("labels", [])]
            label_str = f" [{', '.join(labels)}]" if labels else ""
            items.append({
                "title": f"GitHub Issue: {title}",
                "url": url,
                "source_name": "GitHub Issues",
                "source_type": "community",
                "published_at": created_at,
                "snippet": f"claude-code issue{label_str} ({comments} comments) — {body or title}"[:300],
                "query_used": "GitHub Issues API",
                "raw_score": comments,
            })
        log.info(f"GitHub Issues: {len(items)} items")
    except Exception as e:
        log.warning(f"GitHub Issues fetch failed: {e}")
    return items


def fetch_lobsters(query: str = "claude code") -> list[dict]:
    """Fetch Lobste.rs stories via JSON search API."""
    items = []
    try:
        resp = requests.get(
            "https://lobste.rs/search.json",
            params={"q": query, "what": "stories", "order": "newest"},
            timeout=TIMEOUT,
            headers={"User-Agent": "ClaudeCodeVeille/2.0"},
        )
        resp.raise_for_status()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        for story in resp.json():
            created = story.get("created_at", "")
            dt = parse_date(created)
            if dt and dt < cutoff:
                continue
            title = story.get("title", "").strip()
            url = story.get("url") or story.get("short_id_url", "")
            score = story.get("score", 0) or 0
            description = story.get("description", "").strip()
            items.append({
                "title": title,
                "url": url,
                "source_name": "Lobste.rs",
                "source_type": "community",
                "published_at": created,
                "snippet": description[:300] or f"Lobste.rs ({score} pts) — {title}",
                "query_used": query,
                "raw_score": score,
            })
        log.info(f"Lobste.rs '{query}': {len(items)} items")
    except Exception as e:
        log.warning(f"Lobste.rs fetch failed: {e}")
    return items


def fetch_medium_rss(query: str = "claude-code") -> list[dict]:
    """Fetch Medium articles via tag RSS feed."""
    items = []
    for tag in [query, "anthropic", "claude-ai"]:
        try:
            rss_url = f"https://medium.com/feed/tag/{tag}"
            resp = requests.get(rss_url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
            for item in root.findall(".//item"):
                title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                pub_date = item.findtext("pubDate", "").strip()
                dt = parse_date(pub_date)
                if dt and dt < cutoff:
                    continue
                description = re.sub(r"<[^>]+>", "", item.findtext("description", ""))[:300]
                items.append({
                    "title": title,
                    "url": link,
                    "source_name": "Medium",
                    "source_type": "community",
                    "published_at": pub_date,
                    "snippet": description.strip() or f"Medium article: {title}",
                    "query_used": tag,
                })
        except Exception as e:
            log.warning(f"Medium RSS '{tag}' failed: {e}")

    # Deduplicate by URL
    seen: set[str] = set()
    unique = []
    for item in items:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)

    log.info(f"Medium RSS: {len(unique)} items")
    return unique


def fetch_github_trending() -> list[dict]:
    """Scrape GitHub trending page for repos mentioning Claude Code (Scrapling)."""
    items = []
    try:
        resp = requests.get(
            "https://github.com/trending?since=daily",
            timeout=TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        try:
            from scrapling.parser import Selector
            page = Selector(resp.text)
            for repo in page.css("article.Box-row"):
                name_el = repo.css("h2 a, h1 a")
                name = name_el.css("::text").get(default="").strip().replace("\n", "").replace(" ", "")
                href = name_el.css("::attr(href)").get(default="")
                description = repo.css("p::text").get(default="").strip()
                stars_text = repo.css("span[id*='repo-stars-counter'], .octicon-star + span::text").get(default="0").strip().replace(",", "")
                combined = (name + " " + description).lower()
                if "claude" not in combined and "anthropic" not in combined:
                    continue
                try:
                    stars = int(stars_text)
                except ValueError:
                    stars = 0
                repo_url = f"https://github.com{href}" if href else ""
                items.append({
                    "title": f"GitHub Trending: {name}",
                    "url": repo_url,
                    "source_name": "GitHub Trending",
                    "source_type": "community",
                    "published_at": None,
                    "snippet": description[:300] or f"Trending repo: {name}",
                    "query_used": "GitHub Trending",
                    "raw_score": stars,
                })
        except ImportError:
            # Scrapling not available — skip this source gracefully
            log.info("GitHub Trending: Scrapling not installed, skipping")
    except Exception as e:
        log.warning(f"GitHub Trending fetch failed: {e}")
    log.info(f"GitHub Trending: {len(items)} items")
    return items


def collect() -> tuple[list[dict], int]:
    """Collect raw items from all sources in parallel. Returns (items, sources_count)."""
    log.info("=== collect() ===")

    # Get Reddit token upfront (blocking, needed before parallel Reddit fetches)
    reddit_client_id = os.environ.get("REDDIT_CLIENT_ID")
    reddit_client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    reddit_token = None
    if reddit_client_id and reddit_client_secret:
        reddit_token = get_reddit_token(reddit_client_id, reddit_client_secret)

    # Build all tasks: (service_key, callable, args)
    tasks: list[tuple[str, object, list]] = [
        ("anthropic_rss",       fetch_anthropic_rss,       []),
        ("anthropic_changelog",  fetch_anthropic_changelog,  []),
        ("github_releases",      fetch_github_releases,      []),
        ("github_discussions",   fetch_github_discussions,   []),
        ("github_trending",      fetch_github_trending,      []),
        ("devto",                fetch_devto,                ["claude code"]),
        ("stackoverflow",        fetch_stackoverflow,        ["claude code"]),
        ("lobsters",             fetch_lobsters,             ["claude code"]),
        ("medium",               fetch_medium_rss,           ["claude-code"]),
    ]
    for query in SEARCH_QUERIES:
        tasks.append((f"hn_{query}",     fetch_hackernews, [query]))
        tasks.append((f"reddit_{query}", fetch_reddit,     [query, reddit_token]))
    for query in SEARCH_QUERIES[:2]:
        tasks.append((f"gn_{query}", fetch_google_news, [query]))

    # Run all fetches in parallel
    fetch_results: dict[str, list[dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        future_to_key = {
            executor.submit(fn, *args): key  # type: ignore[arg-type]
            for key, fn, args in tasks
        }
        for future in concurrent.futures.as_completed(future_to_key):
            key = future_to_key[future]
            try:
                fetch_results[key] = future.result()
            except Exception as e:
                log.warning(f"Fetch failed ({key}): {e}")
                fetch_results[key] = []

    # Aggregate results and count active sources (by service, not by query)
    all_items: list[dict] = []
    sources_count = 0
    hn_counted = reddit_counted = gn_counted = False
    new_sources_counted: set[str] = set()

    for key, _fn, _args in tasks:
        items = fetch_results.get(key, [])
        all_items.extend(items)
        if not items:
            continue
        if key in ("anthropic_rss", "anthropic_changelog", "github_releases",
                   "github_discussions", "github_trending"):
            sources_count += 1
        elif key in ("devto", "stackoverflow", "lobsters", "medium"):
            if key not in new_sources_counted:
                new_sources_counted.add(key)
                sources_count += 1
        elif key.startswith("hn_") and not hn_counted:
            hn_counted = True
            sources_count += 1
        elif key.startswith("reddit_") and not reddit_counted:
            reddit_counted = True
            sources_count += 1
        elif key.startswith("gn_") and not gn_counted:
            gn_counted = True
            sources_count += 1

    log.info(f"Total collected: {len(all_items)} items from {sources_count} sources")
    return all_items, sources_count


# ─── Normalize ────────────────────────────────────────────────────────────────

def parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse various date formats into a timezone-aware datetime."""
    if not date_str:
        return None
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def resolve_url(url: str) -> str:
    """Follow redirects to get the final URL (HEAD with GET fallback)."""
    try:
        resp = requests.head(url, allow_redirects=True, timeout=5)
        final = resp.url
        if final == url:
            resp = requests.get(url, allow_redirects=True, timeout=5, stream=True)
            resp.close()
            final = resp.url
        return final
    except Exception:
        return url


def resolve_google_news_urls(items: list["NewsItem"]) -> list["NewsItem"]:
    """Resolve Google News redirect URLs to their final destinations in parallel."""
    to_resolve = [
        (i, item) for i, item in enumerate(items)
        if item.source_name == "Google News" and "news.google.com" in item.url
    ][:30]

    if not to_resolve:
        return items

    log.info(f"Resolving {len(to_resolve)} Google News URLs...")
    result = list(items)
    original_urls = {i: item.url for i, item in to_resolve}

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_idx = {executor.submit(resolve_url, item.url): i for i, item in to_resolve}
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                result[idx].url = future.result()
            except Exception:
                pass

    resolved = sum(1 for i in original_urls if result[i].url != original_urls[i])
    log.info(f"Google News URLs resolved: {resolved}/{len(to_resolve)}")
    return result


def normalize(raw_items: list[dict]) -> list[NewsItem]:
    """Convert raw dicts to normalized NewsItem objects."""
    log.info("=== normalize() ===")
    items = []
    for raw in raw_items:
        title = raw.get("title", "").strip()
        url = raw.get("url", "").strip()
        if not title or not url:
            continue
        dt = parse_date(raw.get("published_at"))
        items.append(
            NewsItem(
                title=title,
                url=url,
                source_name=raw.get("source_name", "Unknown"),
                source_type=raw.get("source_type", "aggregator"),
                published_at=dt.isoformat() if dt else None,
                snippet=raw.get("snippet", "").strip()[:300],
                query_used=raw.get("query_used", ""),
                raw_score=raw.get("raw_score", 0) or 0,
            )
        )
    log.info(f"Normalized: {len(items)} items")
    return items


# ─── Deduplicate ──────────────────────────────────────────────────────────────

def deduplicate(
    items: list[NewsItem], seen_cache: Optional[dict[str, str]] = None
) -> list[NewsItem]:
    """Remove duplicates by exact URL, similar titles, and inter-run cache."""
    log.info("=== deduplicate() ===")
    seen_urls: set[str] = set()
    seen_titles: list[str] = []
    result: list[NewsItem] = []
    cache_hits = 0

    for item in items:
        norm_url = re.sub(r"[?&](utm_[^&]*|ref=[^&]*|source=[^&]*)(&|$)", "", item.url.lower().rstrip("/")).rstrip("?&")
        norm_url = re.sub(r"^http://", "https://", norm_url)
        norm_url = re.sub(r"^https?://www\.", "https://", norm_url)

        if seen_cache and hash_url(norm_url) in seen_cache:
            cache_hits += 1
            continue

        if norm_url in seen_urls:
            continue
        seen_urls.add(norm_url)

        title_lower = item.title.lower()
        if any(
            SequenceMatcher(None, title_lower, t).ratio() >= 0.85
            for t in seen_titles
        ):
            continue

        seen_titles.append(title_lower)
        result.append(item)

    if cache_hits:
        log.info(f"Cache: skipped {cache_hits} already-seen items from previous runs")
    log.info(f"After dedup: {len(result)} items (removed {len(items) - len(result)})")
    return result


# ─── Score and Filter ─────────────────────────────────────────────────────────

def is_relevant(item: NewsItem) -> bool:
    """Return True if the item passes the relevance filter."""
    title_lower = item.title.lower()
    snippet_lower = item.snippet.lower()

    if item.source_type != "official":
        has_term = any(term in title_lower or term in snippet_lower for term in RELEVANCE_TERMS)
        has_anthropic_code = (
            "anthropic" in title_lower and "code" in title_lower
        ) or (
            "anthropic" in snippet_lower and "code" in snippet_lower
        )
        if not (has_term or has_anthropic_code):
            return False

    if any(marker in title_lower or marker in snippet_lower for marker in SEO_NOISE_MARKERS):
        return False

    if item.published_at:
        dt = parse_date(item.published_at)
        if dt and dt < datetime.now(timezone.utc) - timedelta(hours=24):
            return False

    return True


def compute_score(item: NewsItem) -> int:
    """Return an integer relevance score 0–100."""
    score = 0
    title_lower = item.title.lower()
    snippet_lower = item.snippet.lower()

    if item.source_type == "official":
        score += 40
    elif item.source_type == "community":
        score += 20

    if "claude code" in title_lower:
        score += 20

    if any(kw in title_lower or kw in snippet_lower for kw in
           ["release", "update", "launch", "changelog", "new feature"]):
        score += 15

    if item.published_at:
        dt = parse_date(item.published_at)
        if dt and dt >= datetime.now(timezone.utc) - timedelta(hours=24):
            score += 5

    # Raw source score boost (HN pts, Reddit score, reactions)
    if item.raw_score > 100:
        score += 10
    elif item.raw_score > 20:
        score += 5

    # Phase 3 — AI importance boost (applied after Gemini enrichment)
    if item.importance == "critical":
        score += 30
    elif item.importance == "notable":
        score += 10

    # Negative sentiment boost (bugs/controverses méritent d'être vus)
    if item.sentiment == "negative":
        score += 5

    return min(score, 100)


def score_and_filter(items: list[NewsItem]) -> list[NewsItem]:
    """Filter irrelevant items, score the rest, keep score ≥ MIN_SCORE."""
    log.info("=== score_and_filter() ===")
    relevant = [item for item in items if is_relevant(item)]
    log.info(f"After relevance filter: {len(relevant)} (was {len(items)})")

    for item in relevant:
        item.score = compute_score(item)

    scored = [item for item in relevant if item.score >= MIN_SCORE]
    scored.sort(key=lambda x: (x.source_type != "official", -x.score))
    log.info(f"After score filter (≥{MIN_SCORE}): {len(scored)} items")
    return scored


# ─── Summarize / LLM ──────────────────────────────────────────────────────────

def call_gemini(items: list[NewsItem], api_key: str) -> Optional[list[dict]]:
    """Call Gemini Flash to classify items with enriched schema. Returns list of dicts or None."""
    payload_items = [
        {"index": i, "title": item.title, "source": item.source_name, "snippet": item.snippet}
        for i, item in enumerate(items[:MAX_ITEMS_FOR_LLM])
    ]
    prompt = (
        'Voici une liste d\'items de veille sur "Claude Code" d\'Anthropic.\n'
        "Réponds UNIQUEMENT avec un objet JSON valide, sans markdown, sans balises code :\n"
        '{"items": [{'
        '"index": 0, '
        '"dominated": false, '
        '"category": "official|tutorial|discussion|noise", '
        '"importance": "critical|notable|minor", '
        '"one_line_summary": "...", '
        '"why_relevant": "...", '
        '"sentiment": "positive|neutral|negative|mixed"'
        '}]}\n\n'
        "Règles :\n"
        '- "noise" = pas spécifiquement lié à Claude Code d\'Anthropic, ou contenu recyclé/vague\n'
        '- "importance": "critical" = nouvelle version, incident majeur, annonce importante d\'Anthropic (max 1-2 par run)\n'
        '- "importance": "notable" = contenu utile pour les développeurs Claude Code\n'
        '- "importance": "minor" = information secondaire\n'
        '- "why_relevant" = 1 phrase expliquant pourquoi c\'est pertinent pour un dev Claude Code\n'
        '- "sentiment" = ton général de l\'article/discussion (positive=enthousiaste, negative=critique/bug, mixed=nuancé)\n'
        "- Ne jamais inventer d'information absente des métadonnées fournies\n"
        '- Si tu n\'es pas sûr de la catégorie, mets "noise"\n\n'
        f"Items :\n{json.dumps(payload_items, ensure_ascii=False, indent=2)}"
    )
    try:
        resp = requests.post(
            GEMINI_API_URL,
            headers={"X-goog-api-key": api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
            },
            timeout=TIMEOUT_LLM,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        log.info(f"Gemini raw response (first 300 chars): {text[:300]!r}")

        text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text.strip())

        parsed = _try_parse_gemini_json(text)
        if parsed is None:
            log.warning("Gemini: could not extract valid JSON from response")
            return None

        result = parsed.get("items", [])
        log.info(f"Gemini parsed: {len(result)} items classified")
        return result

    except KeyError as e:
        log.warning(f"Gemini: unexpected response structure, missing key {e}")
    except Exception as e:
        log.warning(f"Gemini API failed: {e}")
    return None


def call_gemini_synthesis(items: list[NewsItem], api_key: str) -> Optional[str]:
    """Call Gemini Flash for a daily editorial synthesis (2-3 sentences in French)."""
    if not items:
        return None
    summaries = [
        f"- [{item.source_name}] {item.one_line_summary or item.title}"
        for item in items[:MAX_ITEMS_IN_MESSAGE]
    ]
    prompt = (
        f"Voici les {len(summaries)} actualités Claude Code retenues aujourd'hui :\n"
        + "\n".join(summaries)
        + "\n\nRédige en français 2-3 phrases maximum qui résument les grandes tendances "
        "de la journée (nouvelles features, bugs connus, adoption, discussions actives). "
        "Ton : direct, informatif, sans bullshit marketing. "
        "Réponds uniquement avec le texte de la synthèse, sans JSON, sans titre, sans introduction."
    )
    try:
        resp = requests.post(
            GEMINI_API_URL,
            headers={"X-goog-api-key": api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 256},
            },
            timeout=TIMEOUT_LLM,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        log.info(f"Gemini synthesis: {text[:100]!r}...")
        return text
    except Exception as e:
        log.warning(f"Gemini synthesis failed: {e}")
        return None


def _try_parse_gemini_json(text: str) -> Optional[dict]:
    """Try multiple strategies to extract a valid JSON object from Gemini's response."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Recover complete items from truncated JSON (supports enriched schema)
    items = re.findall(
        r'\{"index"\s*:\s*\d+\s*,\s*"dominated"\s*:\s*(?:true|false)\s*,'
        r'[^}]*"category"\s*:\s*"[^"]*"[^}]*"one_line_summary"\s*:\s*"[^"]*"[^}]*\}',
        text,
    )
    if items:
        log.info(f"Gemini: recovered {len(items)} complete items from truncated JSON")
        recovered = []
        for item_str in items:
            try:
                recovered.append(json.loads(item_str))
            except json.JSONDecodeError:
                continue
        if recovered:
            return {"items": recovered}

    return None


def summarize(
    items: list[NewsItem], llm_api_key: Optional[str]
) -> tuple[list[NewsItem], Optional[str], list[NewsItem]]:
    """Use LLM to filter noise (optional), then return (top_items, synthesis, breaking_items)."""
    log.info("=== summarize() ===")
    breaking_items: list[NewsItem] = []

    if llm_api_key and items:
        log.info(f"Calling Gemini Flash for {min(len(items), MAX_ITEMS_FOR_LLM)} items")
        llm_results = call_gemini(items, llm_api_key)
        if llm_results is not None:
            llm_map = {r["index"]: r for r in llm_results}
            filtered = []
            for i, item in enumerate(items[:MAX_ITEMS_FOR_LLM]):
                info = llm_map.get(i, {})
                if not info.get("dominated") and info.get("category") != "noise":
                    item.category = info.get("category", "")
                    item.one_line_summary = info.get("one_line_summary", "")
                    item.importance = info.get("importance", "minor")
                    item.sentiment = info.get("sentiment", "neutral")
                    item.why_relevant = info.get("why_relevant", "")
                    # Re-score with AI boost
                    item.score = compute_score(item)
                    filtered.append(item)
            log.info(f"After LLM filter: {len(filtered)} items")
            items = filtered
        else:
            log.info("LLM unavailable — fallback to Python scoring only")
    else:
        log.info("LLM step skipped (no key or no items)")

    # Official first, then by score
    items.sort(key=lambda x: (x.source_type != "official", -x.score))
    top_items = items[:MAX_ITEMS_IN_MESSAGE]

    # Identify breaking news (critical items)
    breaking_items = [item for item in top_items if item.importance == "critical"]

    # Generate synthesis
    synthesis: Optional[str] = None
    if llm_api_key and top_items:
        synthesis = call_gemini_synthesis(top_items, llm_api_key)

    return top_items, synthesis, breaking_items


# ─── Format and Send ──────────────────────────────────────────────────────────

def escape_markdown(text: str) -> str:
    """Escape Telegram legacy Markdown special characters in user-provided text."""
    return re.sub(r"([*_`\[\]])", r"\\\1", text)


def _importance_badge(item: NewsItem) -> str:
    """Return a badge prefix for critical items."""
    if item.importance == "critical":
        return "🔥 "
    return ""


def _source_label(item: NewsItem) -> str:
    """Return a bracketed source label with raw score if available."""
    if item.raw_score and item.raw_score > 0:
        score_str = f"{item.raw_score:,}".replace(",", " ")
        return f"[{item.source_name} {score_str}pts] "
    return f"[{item.source_name}] " if item.source_name not in ("Anthropic Blog", "Anthropic Changelog", "GitHub Releases") else ""


def _build_sections(items: list[NewsItem], bold_open: str, bold_close: str) -> list[str]:
    """Build message section lines with the given bold markers."""
    lines = []
    official = [i for i in items if i.source_type == "official"]
    community = [i for i in items if i.source_type == "community"]
    aggregator = [i for i in items if i.source_type == "aggregator"]

    if official:
        lines.append(f"🚨 {bold_open}Officiel{bold_close}")
        for item in official:
            badge = _importance_badge(item)
            summary = escape_markdown(item.one_line_summary or item.title)
            lines.append(f"• {badge}{summary}")
            lines.append(f"  → {item.url}")

    if community:
        lines.append(f"\n💬 {bold_open}Communauté{bold_close}")
        for item in community:
            badge = _importance_badge(item)
            label = _source_label(item)
            summary = escape_markdown(item.one_line_summary or item.title)
            lines.append(f"• {badge}{label}{summary}")
            lines.append(f"  → {item.url}")

    if aggregator:
        lines.append(f"\n📰 {bold_open}Actualités{bold_close}")
        for item in aggregator:
            badge = _importance_badge(item)
            label = _source_label(item)
            summary = escape_markdown(item.one_line_summary or item.title)
            lines.append(f"• {badge}{label}{summary}")
            lines.append(f"  → {item.url}")

    return lines


def format_message(
    items: list[NewsItem],
    total_collected: int,
    sources_count: int,
    synthesis: Optional[str] = None,
) -> str:
    """Build the Telegram-formatted message."""
    today = datetime.now(ZoneInfo("Europe/Paris")).strftime("%d %B %Y")

    if not items:
        return "☕ Rien de neuf sur Claude Code aujourd'hui. Bonne journée !"

    lines = [f"📡 *Veille Claude Code — {today}*\n"]

    if synthesis:
        lines.append(f"💡 {escape_markdown(synthesis)}\n")

    lines.extend(_build_sections(items, "*", "*"))
    lines.append(
        f"\n📊 {sources_count} sources analysées · "
        f"{total_collected} items collectés · {len(items)} retenus"
    )

    message = "\n".join(lines)
    if len(message) > 3500:
        message = message[:3497] + "..."
    return message


def format_discord_message(
    items: list[NewsItem],
    total_collected: int,
    sources_count: int,
    synthesis: Optional[str] = None,
) -> str:
    """Build the Discord-formatted message (uses ** bold, 2000 char limit)."""
    today = datetime.now(ZoneInfo("Europe/Paris")).strftime("%d %B %Y")

    if not items:
        return "☕ Rien de neuf sur Claude Code aujourd'hui. Bonne journée !"

    lines = [f"📡 **Veille Claude Code — {today}**\n"]

    if synthesis:
        lines.append(f"💡 {synthesis}\n")

    lines.extend(_build_sections(items, "**", "**"))
    lines.append(
        f"\n📊 {sources_count} sources analysées · "
        f"{total_collected} items collectés · {len(items)} retenus"
    )

    message = "\n".join(lines)
    if len(message) > 1900:
        message = message[:1897] + "..."
    return message


def format_breaking_message(item: NewsItem) -> str:
    """Format a breaking news alert message."""
    return f"⚡ *BREAKING* — {escape_markdown(item.title)}\n→ {item.url}"


def format_breaking_discord(item: NewsItem) -> str:
    """Format a breaking news alert message for Discord."""
    return f"⚡ **BREAKING** — {item.title}\n→ {item.url}"


def send_telegram(
    message: str, bot_token: str, chat_id: str, dry_run: bool = False
) -> None:
    """Send message via Telegram Bot API. Raises RuntimeError on failure."""
    if dry_run:
        log.info("=== DRY RUN — Message Telegram ===\n%s\n=== END DRY RUN ===", message)
        return

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        log.info("Telegram message sent successfully")
    except Exception as e:
        raise RuntimeError(f"Telegram send failed: {e}") from e


def send_discord(
    message: str, webhook_url: str, dry_run: bool = False
) -> None:
    """Send message via Discord Webhook. Non-blocking on failure."""
    if dry_run:
        log.info("=== DRY RUN — Message Discord ===\n%s\n=== END DRY RUN ===", message)
        return

    try:
        resp = requests.post(
            webhook_url,
            json={"content": message},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        log.info("Discord message sent successfully")
    except Exception as e:
        log.error(f"Failed to send Discord message: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

# ─── Cache inter-runs (GitHub Gist) ──────────────────────────────────────────

def hash_url(url: str) -> str:
    """Return a 16-char hex hash of a normalized URL."""
    return hashlib.sha256(url.lower().rstrip("/").encode()).hexdigest()[:16]


def load_cache(gist_id: str, token: str) -> dict[str, str]:
    """Load the seen-URLs cache from a private GitHub Gist. Returns {} on failure."""
    try:
        resp = requests.get(
            f"https://api.github.com/gists/{gist_id}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        content = resp.json()["files"]["cache.json"]["content"]
        data = json.loads(content).get("seen", {})

        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        pruned = {h: ts for h, ts in data.items() if ts >= cutoff}
        if len(pruned) < len(data):
            log.info(f"Cache: pruned {len(data) - len(pruned)} expired entries")
        log.info(f"Cache loaded: {len(pruned)} URLs already seen")
        return pruned
    except Exception as e:
        log.warning(f"Cache load failed (continuing without cache): {e}")
        return {}


def save_cache(gist_id: str, token: str, seen: dict[str, str]) -> None:
    """Save the updated seen-URLs cache back to the Gist."""
    try:
        payload = {"files": {"cache.json": {"content": json.dumps({"seen": seen}, indent=2)}}}
        resp = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            json=payload,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        log.info(f"Cache saved: {len(seen)} URLs total")
    except Exception as e:
        log.warning(f"Cache save failed: {e}")


def normalize_url(url: str) -> str:
    """Normalize URL for dedup: lowercase, strip trailing slash, remove UTM params."""
    url = url.lower().rstrip("/")
    url = re.sub(r"[?&](utm_[^&]*|ref=[^&]*|source=[^&]*)(&|$)", "", url)
    url = url.rstrip("?&")
    return url


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot de veille Claude Code")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Exécute le pipeline complet mais affiche le message au lieu de l'envoyer",
    )
    args = parser.parse_args()

    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not is_manual and not args.dry_run:
        paris_now = datetime.now(ZoneInfo("Europe/Paris"))
        if not (7 <= paris_now.hour <= 10):
            log.info(f"Paris time is {paris_now.hour}h (outside 7–10h window) — skipping this cron fire.")
            sys.exit(0)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    llm_api_key = os.environ.get("LLM_API_KEY")
    cache_gist_id = os.environ.get("CACHE_GIST_ID")
    cache_github_token = os.environ.get("CACHE_GITHUB_TOKEN")
    discord_webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

    if not args.dry_run:
        missing = [name for name, val in [("TELEGRAM_BOT_TOKEN", bot_token), ("TELEGRAM_CHAT_ID", chat_id)] if not val]
        if missing:
            log.error(f"Missing required environment variables: {', '.join(missing)}")
            sys.exit(1)

    if not llm_api_key:
        log.warning("LLM_API_KEY not set — LLM filtering disabled")

    pipeline_start = time.time()
    log.info("Starting Claude Code veille pipeline")

    seen_cache: dict[str, str] = {}
    if cache_gist_id and cache_github_token:
        seen_cache = load_cache(cache_gist_id, cache_github_token)
    else:
        log.info("Cache disabled (CACHE_GIST_ID or CACHE_GITHUB_TOKEN not set)")

    raw_items, sources_count = collect()
    total_collected = len(raw_items)

    normalized = normalize(raw_items)
    normalized = resolve_google_news_urls(normalized)
    deduped = deduplicate(normalized, seen_cache=seen_cache)
    scored = score_and_filter(deduped)
    final_items, synthesis, breaking_items = summarize(scored, llm_api_key)

    # Send breaking news alerts first (before the full digest)
    if breaking_items:
        for breaking_item in breaking_items:
            log.info(f"Breaking news detected: {breaking_item.title}")
            breaking_tg = format_breaking_message(breaking_item)
            try:
                send_telegram(breaking_tg, bot_token, chat_id, dry_run=args.dry_run)
            except RuntimeError as e:
                log.error(f"Breaking news Telegram send failed: {e}")
            if discord_webhook_url:
                breaking_dc = format_breaking_discord(breaking_item)
                send_discord(breaking_dc, discord_webhook_url, dry_run=args.dry_run)

    # Send main digest
    telegram_message = format_message(final_items, total_collected, sources_count, synthesis)
    telegram_ok = True
    try:
        send_telegram(telegram_message, bot_token, chat_id, dry_run=args.dry_run)
    except RuntimeError as e:
        log.error(str(e))
        telegram_ok = False

    if discord_webhook_url:
        discord_message = format_discord_message(final_items, total_collected, sources_count, synthesis)
        send_discord(discord_message, discord_webhook_url, dry_run=args.dry_run)

    # Update inter-run cache
    items_to_cache = scored
    if cache_gist_id and cache_github_token and items_to_cache:
        now_iso = datetime.now(timezone.utc).isoformat()
        for item in items_to_cache:
            seen_cache[hash_url(normalize_url(item.url))] = now_iso
        if not args.dry_run:
            save_cache(cache_gist_id, cache_github_token, seen_cache)

    elapsed = time.time() - pipeline_start
    log.info(f"Pipeline completed in {elapsed:.1f}s")

    if not telegram_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
