#!/usr/bin/env python3
"""
Bot Telegram de veille "Claude Code" — MVP
Pipeline: collect → normalize → deduplicate → score_and_filter → summarize → send_telegram
"""

import argparse
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

        # Step 1: collect unique /blog/<slug> URLs (excluding category pages)
        slugs_seen: set[str] = set()
        blog_urls: list[tuple[str, str]] = []  # (url, slug)
        for slug in re.findall(r'href="(/blog/([^"#?/]+))"', content):
            path, name = slug
            if name and name not in slugs_seen:
                slugs_seen.add(name)
                blog_urls.append((f"https://www.anthropic.com{path}", name))

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


def fetch_reddit(query: str) -> list[dict]:
    """Fetch recent Reddit posts. Tries www then old.reddit.com (GitHub Actions IPs are often blocked)."""
    items = []
    params = {"q": query, "sort": "new", "t": "day", "limit": 25}
    # Reddit blocks most datacenter IPs; a realistic browser UA improves success rate.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }

    for base in ["https://www.reddit.com/search.json", "https://old.reddit.com/search.json"]:
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


def collect() -> tuple[list[dict], int]:
    """Collect raw items from all sources. Returns (items, sources_count)."""
    log.info("=== collect() ===")
    all_items: list[dict] = []
    sources_count = 0

    # Priority 1 — Official
    for fetcher in [fetch_anthropic_rss, fetch_anthropic_changelog]:
        result = fetcher()
        if result:
            sources_count += 1
        all_items.extend(result)

    # Priority 2 — Community (one source counter per service, not per query)
    hn_contributed = False
    reddit_contributed = False
    for query in SEARCH_QUERIES:
        hn_items = fetch_hackernews(query)
        if hn_items and not hn_contributed:
            sources_count += 1
            hn_contributed = True
        all_items.extend(hn_items)

        reddit_items = fetch_reddit(query)
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

def deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    """Remove duplicates by exact URL and similar titles (SequenceMatcher ≥ 0.85)."""
    log.info("=== deduplicate() ===")
    seen_urls: set[str] = set()
    seen_titles: list[str] = []
    result: list[NewsItem] = []

    for item in items:
        norm_url = item.url.rstrip("/").lower()
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
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        log.info(f"Gemini raw response (first 300 chars): {text[:300]!r}")

        # Strip markdown code fences if present (```json ... ``` or ``` ... ```)
        text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text.strip())

        # Extract the JSON object
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            log.warning(f"Gemini: no JSON object found in response: {text[:200]!r}")
            return None

        parsed = json.loads(match.group())
        result = parsed.get("items", [])
        log.info(f"Gemini parsed: {len(result)} items classified")
        return result

    except json.JSONDecodeError as e:
        log.warning(f"Gemini: JSON parse error: {e}")
    except KeyError as e:
        log.warning(f"Gemini: unexpected response structure, missing key {e}")
    except Exception as e:
        log.warning(f"Gemini API failed: {e}")
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

def main() -> None:
    parser = argparse.ArgumentParser(description="Bot de veille Claude Code")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Exécute le pipeline complet mais affiche le message au lieu de l'envoyer",
    )
    args = parser.parse_args()

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    llm_api_key = os.environ.get("LLM_API_KEY")

    if not args.dry_run:
        missing = [v for v, k in [("TELEGRAM_BOT_TOKEN", bot_token), ("TELEGRAM_CHAT_ID", chat_id)] if not k]
        if missing:
            log.error(f"Missing required environment variables: {', '.join(missing)}")
            sys.exit(1)

    if not llm_api_key:
        log.warning("LLM_API_KEY not set — LLM filtering disabled")

    log.info("Starting Claude Code veille pipeline")

    raw_items, sources_count = collect()
    total_collected = len(raw_items)

    normalized = normalize(raw_items)
    deduped = deduplicate(normalized)
    scored = score_and_filter(deduped)
    final_items = summarize(scored, llm_api_key)

    message = format_message(final_items, total_collected, sources_count)
    send_telegram(message, bot_token, chat_id, dry_run=args.dry_run)

    log.info("Pipeline completed successfully")


if __name__ == "__main__":
    main()
