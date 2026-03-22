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

TIMEOUT = 15  # seconds

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

    # Fallback: scrape the blog page
    log.info("Anthropic RSS not found — scraping blog page")
    try:
        resp = requests.get(
            "https://www.anthropic.com/blog",
            timeout=TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        content = resp.text

        # Step 1: collect unique /blog/<slug> URLs — handle relative & absolute, single & double quotes
        slugs_seen: set[str] = set()
        blog_urls: list[tuple[str, str]] = []  # (url, slug)
        for path in re.findall(
            r'''href=["'](?:https://www\.anthropic\.com)?(/blog/([a-z0-9][a-z0-9-]{2,}))["']''',
            content,
        ):
            full_path, name = path
            if name and name not in slugs_seen:
                slugs_seen.add(name)
                blog_urls.append((f"https://www.anthropic.com{full_path}", name))

        # Step 2: collect page headings (h1/h2/h3) as candidate titles
        headings = [
            re.sub(r"\s+", " ", h).strip()
            for h in re.findall(r"<h[123][^>]*>([^<]{10,150})</h[123]>", content)
            if h.strip()
        ]

        # Step 3: pair urls with headings positionally, fallback to formatted slug
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
        log.info(f"Anthropic Blog scrape: {len(items)} items ({len(headings)} headings found)")
    except Exception as e:
        log.warning(f"Anthropic Blog scrape failed: {e}")

    return items


def fetch_anthropic_changelog() -> list[dict]:
    """Fetch Anthropic changelog page via light scraping."""
    url = "https://docs.anthropic.com/en/docs/changelog"
    items = []
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        content = resp.text
        # Extract headings as changelog entries
        headings = re.findall(r"<h[23][^>]*>([^<]+)</h[23]>", content)
        for heading in headings[:10]:
            heading = heading.strip()
            if not heading:
                continue
            items.append({
                "title": f"Anthropic Changelog: {heading}",
                "url": url,
                "source_name": "Anthropic Changelog",
                "source_type": "official",
                "published_at": None,
                "snippet": f"Entry from Anthropic docs changelog: {heading}"[:300],
                "query_used": "Anthropic Changelog",
            })
        log.info(f"Anthropic Changelog: {len(items)} items")
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
            points = hit.get("points", 0)
            created_at = hit.get("created_at", "")
            items.append({
                "title": title,
                "url": story_url,
                "source_name": "Hacker News",
                "source_type": "community",
                "published_at": created_at,
                "snippet": f"HN ({points} pts) — {title}"[:300],
                "query_used": query,
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
    """Fetch recent Reddit posts. Uses OAuth if token provided, else falls back to public API."""
    items = []
    params = {"q": query, "sort": "new", "t": "day", "limit": 25}

    if bearer_token:
        # OAuth path — authenticated, no IP block
        bases = ["https://oauth.reddit.com/search"]
        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "User-Agent": "python:claude-code-veille:v2.0",
            "Accept": "application/json",
        }
    else:
        # Fallback — unauthenticated (likely blocked from GitHub Actions)
        bases = ["https://www.reddit.com/search.json", "https://old.reddit.com/search.json"]
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/json",
        }

    for base in bases:
        try:
            resp = requests.get(base, params=params, timeout=TIMEOUT, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            for post in data.get("data", {}).get("children", []):
                p = post.get("data", {})
                title = p.get("title", "").strip()
                permalink = f"https://reddit.com{p.get('permalink', '')}"
                selftext = p.get("selftext", "")[:200]
                created_utc = p.get("created_utc")
                pub_date = (
                    datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat()
                    if created_utc
                    else None
                )
                subreddit = p.get("subreddit_name_prefixed", "")
                items.append({
                    "title": title,
                    "url": permalink,
                    "source_name": "Reddit",
                    "source_type": "community",
                    "published_at": pub_date,
                    "snippet": f"{subreddit} — {selftext or title}"[:300],
                    "query_used": query,
                })
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
        resp = requests.get(
            url,
            params={"per_page": 10},
            timeout=TIMEOUT,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "ClaudeCodeVeille/2.0"},
        )
        resp.raise_for_status()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        for release in resp.json():
            published_at = release.get("published_at", "")
            dt = parse_date(published_at)
            if dt and dt < cutoff:
                continue  # plus vieux que 48h
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


def collect() -> tuple[list[dict], int]:
    """Collect raw items from all sources. Returns (items, sources_count)."""
    log.info("=== collect() ===")
    all_items: list[dict] = []
    sources_count = 0

    # Priority 1 — Official
    for fetcher in [fetch_anthropic_rss, fetch_anthropic_changelog, fetch_github_releases]:
        result = fetcher()
        if result:
            sources_count += 1
        all_items.extend(result)

    # Priority 2 — Community (one source counter per service, not per query)
    reddit_client_id = os.environ.get("REDDIT_CLIENT_ID")
    reddit_client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    reddit_token = None
    if reddit_client_id and reddit_client_secret:
        reddit_token = get_reddit_token(reddit_client_id, reddit_client_secret)

    hn_contributed = False
    reddit_contributed = False
    for query in SEARCH_QUERIES:
        hn_items = fetch_hackernews(query)
        if hn_items and not hn_contributed:
            sources_count += 1
            hn_contributed = True
        all_items.extend(hn_items)

        reddit_items = fetch_reddit(query, bearer_token=reddit_token)
        if reddit_items and not reddit_contributed:
            sources_count += 1
            reddit_contributed = True
        all_items.extend(reddit_items)

    # Priority 3 — Aggregator (first two queries to avoid hammering)
    gn_contributed = False
    for query in SEARCH_QUERIES[:2]:
        gn_items = fetch_google_news(query)
        if gn_items and not gn_contributed:
            sources_count += 1
            gn_contributed = True
        all_items.extend(gn_items)

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
        # If HEAD didn't redirect at all, try GET (some servers ignore HEAD)
        if final == url:
            resp = requests.get(url, allow_redirects=True, timeout=5, stream=True)
            resp.close()
            final = resp.url
        return final
    except Exception:
        return url


def resolve_google_news_urls(items: list[NewsItem]) -> list[NewsItem]:
    """Resolve Google News redirect URLs to their final destinations in parallel."""
    to_resolve = [
        (i, item) for i, item in enumerate(items)
        if item.source_name == "Google News" and "news.google.com" in item.url
    ][:30]  # max 30 pour limiter la latence

    if not to_resolve:
        return items

    log.info(f"Resolving {len(to_resolve)} Google News URLs...")
    result = list(items)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_idx = {executor.submit(resolve_url, item.url): i for i, item in to_resolve}
        for future in concurrent.futures.as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                result[idx].url = future.result()
            except Exception:
                pass  # conserver l'URL d'origine

    resolved = sum(1 for i, item in to_resolve if result[i].url != item.url)
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
        norm_url = item.url.rstrip("/").lower()

        # Inter-run cache check
        if seen_cache and hash_url(norm_url) in seen_cache:
            cache_hits += 1
            continue

        # Exact URL dedup
        if norm_url in seen_urls:
            continue
        seen_urls.add(norm_url)

        # Fuzzy title dedup
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

    # Relevance gate — official sources pass directly
    if item.source_type != "official":
        has_term = any(term in title_lower for term in RELEVANCE_TERMS)
        has_anthropic_code = "anthropic" in title_lower and "code" in title_lower
        if not (has_term or has_anthropic_code):
            return False

    # SEO noise exclusion
    if any(marker in title_lower or marker in snippet_lower for marker in SEO_NOISE_MARKERS):
        return False

    # Age filter
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
    """Call Gemini Flash to classify items. Returns list of dicts or None on failure."""
    payload_items = [
        {"index": i, "title": item.title, "source": item.source_name, "snippet": item.snippet}
        for i, item in enumerate(items[:MAX_ITEMS_FOR_LLM])
    ]
    prompt = (
        'Voici une liste d\'items de veille sur "Claude Code" d\'Anthropic.\n'
        "Réponds UNIQUEMENT avec un objet JSON valide, sans markdown, sans balises code :\n"
        '{"items": [{"index": 0, "dominated": false, '
        '"category": "official|tutorial|discussion|noise", "one_line_summary": "..."}]}\n\n'
        "Règles :\n"
        '- "noise" = pas spécifiquement lié à Claude Code d\'Anthropic, ou contenu recyclé/vague\n'
        "- Ne jamais inventer d'information absente des métadonnées fournies\n"
        '- Si tu n\'es pas sûr, mets "noise"\n\n'
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
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        log.info(f"Gemini raw response (first 300 chars): {text[:300]!r}")

        # Strip markdown code fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text.strip())

        parsed = _try_parse_gemini_json(text)
        if parsed is None:
            log.warning(f"Gemini: could not extract valid JSON from response")
            return None

        result = parsed.get("items", [])
        log.info(f"Gemini parsed: {len(result)} items classified")
        return result

    except KeyError as e:
        log.warning(f"Gemini: unexpected response structure, missing key {e}")
    except Exception as e:
        log.warning(f"Gemini API failed: {e}")
    return None


def _try_parse_gemini_json(text: str) -> Optional[dict]:
    """Try multiple strategies to extract a valid JSON object from Gemini's response."""
    # Strategy 1: parse the whole text directly
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: find the outermost {...} block and parse it
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Strategy 3: the JSON is truncated — recover complete items only
    # Find all well-formed item objects before the truncation point
    items = re.findall(
        r'\{"index"\s*:\s*\d+\s*,\s*"dominated"\s*:\s*(?:true|false)\s*,'
        r'\s*"category"\s*:\s*"[^"]*"\s*,\s*"one_line_summary"\s*:\s*"[^"]*"\s*\}',
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


def summarize(items: list[NewsItem], llm_api_key: Optional[str]) -> list[NewsItem]:
    """Use LLM to filter noise (optional), then return top MAX_ITEMS_IN_MESSAGE items."""
    log.info("=== summarize() ===")

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
                    filtered.append(item)
            log.info(f"After LLM filter: {len(filtered)} items")
            items = filtered
        else:
            log.info("LLM unavailable — fallback to Python scoring only")
    else:
        log.info("LLM step skipped (no key or no items)")

    # Official first, then by score
    items.sort(key=lambda x: (x.source_type != "official", -x.score))
    return items[:MAX_ITEMS_IN_MESSAGE]


# ─── Format and Send ──────────────────────────────────────────────────────────

def format_message(items: list[NewsItem], total_collected: int, sources_count: int) -> str:
    """Build the Telegram-formatted message."""
    today = datetime.now().strftime("%d %B %Y")

    if not items:
        return "☕ Rien de neuf sur Claude Code aujourd'hui. Bonne journée !"

    lines = [f"📡 *Veille Claude Code — {today}*\n"]

    official = [i for i in items if i.source_type == "official"]
    community = [i for i in items if i.source_type == "community"]
    aggregator = [i for i in items if i.source_type == "aggregator"]

    if official:
        lines.append("🚨 *Officiel*")
        for item in official:
            summary = item.one_line_summary or item.title
            lines.append(f"• {summary}")
            lines.append(f"  → {item.url}")

    if community:
        lines.append("\n💬 *Communauté*")
        for item in community:
            summary = item.one_line_summary or item.title
            lines.append(f"• {summary}")
            lines.append(f"  → {item.url}")

    if aggregator:
        lines.append("\n📰 *Actualités*")
        for item in aggregator:
            summary = item.one_line_summary or item.title
            lines.append(f"• {summary}")
            lines.append(f"  → {item.url}")

    lines.append(
        f"\n📊 {sources_count} sources analysées · "
        f"{total_collected} items collectés · {len(items)} retenus"
    )

    message = "\n".join(lines)
    if len(message) > 1500:
        message = message[:1497] + "..."
    return message


def send_telegram(
    message: str, bot_token: str, chat_id: str, dry_run: bool = False
) -> None:
    """Send message via Telegram Bot API. Exits with code 1 on failure."""
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
        log.error(f"Failed to send Telegram message: {e}")
        sys.exit(1)


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

        # TTL: purge entries older than 7 days
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot de veille Claude Code")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Exécute le pipeline complet mais affiche le message au lieu de l'envoyer",
    )
    args = parser.parse_args()

    # Évol 4 — Skip si le cron de 6h UTC fire en hiver (déjà 7h UTC = 8h Paris)
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if not is_manual and not args.dry_run:
        paris_now = datetime.now(ZoneInfo("Europe/Paris"))
        if paris_now.hour != 8:
            log.info(f"Paris time is {paris_now.hour}h (not 8h) — skipping this cron fire.")
            sys.exit(0)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    llm_api_key = os.environ.get("LLM_API_KEY")
    cache_gist_id = os.environ.get("CACHE_GIST_ID")
    cache_github_token = os.environ.get("CACHE_GITHUB_TOKEN")

    if not args.dry_run:
        missing = [v for v, k in [("TELEGRAM_BOT_TOKEN", bot_token), ("TELEGRAM_CHAT_ID", chat_id)] if not k]
        if missing:
            log.error(f"Missing required environment variables: {', '.join(missing)}")
            sys.exit(1)

    if not llm_api_key:
        log.warning("LLM_API_KEY not set — LLM filtering disabled")

    log.info("Starting Claude Code veille pipeline")

    # Load inter-run cache
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
    final_items = summarize(scored, llm_api_key)

    message = format_message(final_items, total_collected, sources_count)
    send_telegram(message, bot_token, chat_id, dry_run=args.dry_run)

    # Update inter-run cache with newly sent URLs
    if cache_gist_id and cache_github_token and final_items:
        now_iso = datetime.now(timezone.utc).isoformat()
        for item in final_items:
            seen_cache[hash_url(item.url.rstrip("/").lower())] = now_iso
        if not args.dry_run:
            save_cache(cache_gist_id, cache_github_token, seen_cache)

    log.info("Pipeline completed successfully")


if __name__ == "__main__":
    main()
