#!/usr/bin/env python3
"""
Anime News -> Discord Webhook Automation
========================================

Collects anime / manga / light-novel news from RSS feeds and posts compact,
de-duplicated Discord embeds through a webhook.

    python bot.py                   run continuously
    python bot.py --once            run a single cycle and exit (cron friendly)
    python bot.py --dry-run         run a cycle, print embeds, post nothing, save nothing
    python bot.py --test            basic checks (config, feeds, sample embed)
    python bot.py --test --send     ... and send one sample embed to Discord
    python bot.py --test-media      media classification test
    python bot.py --test-duplicates duplicate detection test

Pipeline:
    fetch -> clean -> classify -> extract (name/event/context/image)
          -> posted-state filter -> duplicate clustering -> rank
          -> post (max N per cycle) -> remember
"""

from __future__ import annotations

import argparse
import calendar
import difflib
import html
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

warnings.filterwarnings("ignore", message=r"The input looks like")
try:  # bs4 >= 4.11
    from bs4 import XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:  # pragma: no cover
    pass

# =============================================================================
# CONFIGURATION
# =============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Feeds are listed in priority order: when several sources report the same
# story, the earlier feed wins. Verify URLs with `python bot.py --test`.
RSS_FEEDS = [
    {"name": "Anime News Network", "url": "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=w"},
    {"name": "Crunchyroll News", "url": "https://cr-news-api-service.prd.crunchyrollsvc.com/v1/en-US/rss"},
    {"name": "MyAnimeList", "url": "https://myanimelist.net/rss/news.xml"},
    {"name": "Anime Corner", "url": "https://animecorner.me/feed/"},
]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


MAX_POSTS_PER_CYCLE = _env_int("MAX_POSTS_PER_CYCLE", 3)
CHECK_INTERVAL_MINUTES = max(1, _env_int("CHECK_INTERVAL_MINUTES", 15))
MAX_ARTICLE_AGE_HOURS = _env_int("MAX_ARTICLE_AGE_HOURS", 48)
FIRST_RUN_POST_LATEST = _env_int("FIRST_RUN_POST_LATEST", 0)  # 0 = seed only, post nothing
ENABLE_OG_IMAGE = _env_bool("ENABLE_OG_IMAGE", True)
REQUEST_TIMEOUT = 15
POST_DELAY_SECONDS = 1.5

POSTED_FILE = os.path.join(BASE_DIR, "posted.json")
DUPLICATE_WINDOW_DAYS = 14  # compare against posted stories from this period
STATE_RETENTION_DAYS = 60
STATE_MAX_ENTRIES = 3000

# Duplicate-detection thresholds
SAME_STORY_SCORE = 0.85  # clearly the same story
CONTEXT_SCORE = 0.71  # same story when contextual signals agree
SAME_SOURCE_SCORE = 0.92  # two articles from ONE site must be near-identical

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_ROLE_ID = os.getenv("DISCORD_ROLE_ID", "").strip()
WEBHOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+/?(?:\?.*)?$")

USER_AGENT = "Mozilla/5.0 (compatible; AnimeNewsDiscordBot/1.0; +RSS reader)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

log = logging.getLogger("animebot")


def redact(text) -> str:
    """Never let the webhook URL leak into logs."""
    text = str(text)
    if DISCORD_WEBHOOK_URL:
        text = text.replace(DISCORD_WEBHOOK_URL, "<webhook>")
    return re.sub(r"(discord(?:app)?\.com/api/webhooks/\d+/)[\w-]+", r"\1<token>", text)


# =============================================================================
# DATA MODEL
# =============================================================================


@dataclass
class Event:
    key: str
    label: str
    priority: int  # lower = more important
    dedup: tuple  # keys used for duplicate comparison
    pos: int = 0
    phrases: list = field(default_factory=list)


@dataclass
class Article:
    title: str
    link: str
    source: str
    description: str = ""
    guid: str = ""
    published: float = 0.0  # epoch seconds, 0 = unknown
    image: str = ""
    author: str = ""
    source_rank: int = 0
    # derived by analyze()
    media: str = "Other"
    scores: dict = field(default_factory=dict)
    media_name: str = ""
    season: str = ""
    numbers: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    context: str = ""
    sig: dict = field(default_factory=dict)


# =============================================================================
# TEXT / URL CLEANING
# =============================================================================

TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src",
    "cmpid", "cid", "source", "spm", "_hsenc", "_hsmi", "yclid", "twclid",
}


def clean_url(url) -> str:
    url = (str(url) if url else "").strip()
    if not url:
        return ""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            return ""
        query = [
            (k, v)
            for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS)
        ]
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(query), ""))
    except ValueError:
        return ""


def link_key(url) -> str:
    """Scheme/www/trailing-slash independent key for comparing links."""
    u = clean_url(url)
    if not u:
        return ""
    p = urlparse(u)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def clean_text(raw) -> str:
    """HTML -> readable single-line text."""
    if not raw:
        return ""
    text = str(raw)
    if "<" in text or "&" in text:
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "iframe", "noscript"]):
            tag.decompose()
        text = soup.get_text(" ")
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"The post .*? appeared first on .*?(?:\.|$)", "", text)
    text = re.sub(r"\[(?:…|\.\.\.)\]", "", text)
    text = re.sub(r"(?:Continue reading|Read more)\b.*$", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text


def shorten(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-–—") + "…"


def split_sentences(text: str) -> list:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“‘])", text)
    return [p.strip() for p in parts if p.strip()]


# =============================================================================
# IMAGE HANDLING
# =============================================================================

BAD_IMAGE_HINTS = ("pixel", "1x1", "spacer", "blank.", "tracking", "beacon", "emoji", "favicon", "gravatar", "avatar")
IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|avif)(?:$|\?)", re.I)


def usable_image_url(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    low = url.lower()
    if re.search(r"\.(?:svg|ico)(?:$|\?)", low):
        return False
    return not any(h in low for h in BAD_IMAGE_HINTS)


def extract_image(entry, base_link: str) -> str:
    cands = []
    for m in entry.get("media_content", []) or []:
        typ, medium = (m.get("type") or ""), (m.get("medium") or "")
        if typ.startswith("video") or medium == "video":
            continue
        if m.get("url"):
            cands.append(m["url"])
    for m in entry.get("media_thumbnail", []) or []:
        if m.get("url"):
            cands.append(m["url"])
    for l in (entry.get("links") or []) + (entry.get("enclosures") or []):
        if l.get("rel") in (None, "enclosure") and (
            (l.get("type") or "").startswith("image") or IMG_EXT_RE.search(l.get("href") or l.get("url") or "")
        ):
            cands.append(l.get("href") or l.get("url"))
    blobs = [entry.get("summary", ""), entry.get("description", "")]
    blobs += [c.get("value", "") for c in entry.get("content", []) or []]
    for blob in blobs:
        if blob and "<img" in blob:
            try:
                for img in BeautifulSoup(blob, "html.parser").find_all("img"):
                    src = img.get("src") or img.get("data-src")
                    if src:
                        cands.append(src)
            except Exception:  # noqa: BLE001
                pass
    for c in cands:
        url = urljoin(base_link or "", html.unescape(str(c)).strip())
        if usable_image_url(url):
            return url
    return ""


def verify_image(url: str) -> bool:
    """True only if the URL answers and serves an image."""
    try:
        r = SESSION.head(url, timeout=6, allow_redirects=True)
        if r.status_code >= 400:
            r = SESSION.get(url, timeout=6, stream=True)
            r.close()
        if r.status_code >= 400:
            return False
        ctype = (r.headers.get("Content-Type") or "").lower()
        return not ctype or ctype.startswith("image/") or "octet-stream" in ctype
    except requests.RequestException:
        return False


def looks_like_challenge(text: str) -> bool:
    low = text[:4000].lower()
    return "just a moment" in low or "cf-chl" in low or "challenge-platform" in low or (
        "attention required" in low and "cloudflare" in low
    )


def fetch_og_image(url: str) -> str:
    """Best-effort Open Graph image lookup (only used for articles we are about to post)."""
    try:
        r = SESSION.get(url, timeout=8, stream=True)
        if r.status_code != 200:
            r.close()
            return ""
        data = b""
        for chunk in r.iter_content(16384):
            data += chunk
            if len(data) > 250_000:
                break
        r.close()
        text = data.decode(r.encoding or "utf-8", "ignore")
        if looks_like_challenge(text):
            return ""
        soup = BeautifulSoup(text, "html.parser")
        for attrs in ({"property": "og:image"}, {"name": "twitter:image"}, {"property": "og:image:url"}):
            tag = soup.find("meta", attrs=attrs)
            if tag and tag.get("content"):
                img = urljoin(url, tag["content"].strip())
                if usable_image_url(img):
                    return img
    except (requests.RequestException, ValueError):
        pass
    return ""


def resolve_image(a: Article) -> str:
    if a.image and verify_image(a.image):
        return a.image
    if ENABLE_OG_IMAGE:
        og = fetch_og_image(a.link)
        if og and verify_image(og):
            return og
    return ""


# =============================================================================
# FEED FETCHING
# =============================================================================


class FeedError(Exception):
    pass


def _feed_markers(head: str) -> bool:
    return any(m in head for m in ("<rss", "<feed", "<rdf:rdf", "<channel"))


def fetch_feed(feed: dict, rank: int = 0) -> list:
    """Download one feed. Raises FeedError with a short reason on failure."""
    headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"}
    resp = None
    last_err = ""
    for attempt in range(2):
        try:
            resp = SESSION.get(feed["url"], headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code < 500:
                break
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            last_err = f"network error ({e.__class__.__name__})"
            resp = None
        time.sleep(2)
    if resp is None:
        raise FeedError(last_err or "no response")

    body = resp.content
    head = body[:4000].decode("utf-8", "ignore").lower()
    if resp.headers.get("cf-mitigated") == "challenge" or (looks_like_challenge(head) and not _feed_markers(head)):
        raise FeedError("Cloudflare challenge page instead of RSS - skipped (no bypass attempted)")
    if resp.status_code >= 400:
        raise FeedError(f"HTTP {resp.status_code}")
    if not _feed_markers(head):
        raise FeedError("response is not RSS/Atom XML")

    parsed = feedparser.parse(body)
    if not parsed.entries:
        if parsed.get("bozo"):
            raise FeedError("invalid RSS/XML")
        return []

    articles = []
    for entry in parsed.entries:
        try:
            art = entry_to_article(entry, feed["name"], rank)
            if art:
                articles.append(art)
        except Exception as e:  # noqa: BLE001 - one broken article must never kill the cycle
            log.warning("[%s] skipped malformed article: %s", feed["name"], e.__class__.__name__)
    return articles


def entry_to_article(entry, source: str, rank: int):
    title = clean_text(entry.get("title"))
    link = clean_url(entry.get("link"))
    if not title or not link:
        return None
    desc_raw = entry.get("summary") or entry.get("description") or ""
    if not desc_raw and entry.get("content"):
        desc_raw = entry["content"][0].get("value", "")
    ts = entry.get("published_parsed") or entry.get("updated_parsed")
    published = float(calendar.timegm(ts)) if ts else 0.0
    return Article(
        title=title,
        link=link,
        source=source,
        description=clean_text(desc_raw),
        guid=str(entry.get("id") or entry.get("guid") or "").strip(),
        published=published,
        image=extract_image(entry, link),
        author=clean_text(entry.get("author")),
        source_rank=rank,
    )


def collect_articles(feeds: list, quiet: bool = False):
    """Fetch every feed independently. Returns (articles, per_feed_status)."""
    all_articles, status = [], []
    for rank, feed in enumerate(feeds):
        try:
            items = fetch_feed(feed, rank)
            status.append((feed["name"], True, f"{len(items)} articles"))
            all_articles.extend(items)
        except FeedError as e:
            status.append((feed["name"], False, str(e)))
            log.warning("[%s] feed skipped: %s", feed["name"], e)
        except Exception as e:  # noqa: BLE001
            status.append((feed["name"], False, f"unexpected error: {e.__class__.__name__}"))
            log.warning("[%s] unexpected feed error: %s", feed["name"], redact(e))
    return all_articles, status


# =============================================================================
# MEDIA TYPE DETECTION (Anime / Manga / Novel / Other)
# =============================================================================


def _c(pattern: str, flags: int = re.I):
    return re.compile(pattern, flags)


ANIME_SIGNALS = [
    (_c(r"\banime\b"), 3.0),
    (_c(r"\btv anime\b"), 1.5),
    (_c(r"\banime adaptation\b"), 1.0),
    (_c(r"\b(?:season\s*\d+|\d+(?:st|nd|rd|th)\s+season|(?:final|second|third|fourth|new)\s+season)\b"), 3.0),
    (_c(r"\bepisodes?\b"), 3.0),
    (_c(r"\btrailers?\b"), 2.0),
    (_c(r"\bteasers?\b"), 2.0),
    (_c(r"\bpv\b|\bpromo(?:tional)? video\b"), 2.0),
    (_c(r"\bkey visual\b"), 2.0),
    (_c(r"\bvoice cast\b|\bvoice actors?\b|\bcasts?\b"), 2.0),
    (_c(r"\bbroadcast\b|\bpremier(?:e|es|ing)\b|\bcour\b|\bsimulcast\b"), 2.0),
    (_c(r"\b(?:opening|ending) (?:theme|song)s?\b|\btheme songs?\b"), 2.0),
    (_c(r"\bstaff\b"), 1.5),
    (_c(r"\bova\b|\boad\b|\banime (?:film|movie)\b"), 2.0),
]
MANGA_SIGNALS = [
    (_c(r"\bmanga\b"), 3.0),
    (_c(r"\bchapters?\b"), 3.0),
    (_c(r"\bserializ(?:ed|ation|es|ing)\b"), 3.0),
    (_c(r"\bvolumes?\b"), 1.0),
    (_c(r"\btankobon\b"), 2.0),
    (_c(r"\bmanga adaptation\b"), 1.0),
    (_c(r"\bnew chapter\b"), 2.0),
    (_c(r"\bmangaka\b"), 2.0),
    (_c(r"\b(?:weekly )?sh[o]+nen jump\b|\bshounen\b"), 1.5),
]
NOVEL_SIGNALS = [
    (_c(r"\blight novels?\b"), 4.0),
    (_c(r"(?<!light )(?<!web )\bnovels?\b"), 2.0),
    (_c(r"\bLNs?\b", 0), 3.0),
    (_c(r"\bweb novels?\b"), 3.0),
    (_c(r"\bnovel adaptation\b"), 2.0),
    (_c(r"\bvolumes?\b"), 1.0),
]

# Event patterns that say what the news is ABOUT (as opposed to the source material).
ANIME_ADAPT_RE = _c(
    r"\b(?:gets?|getting|receives?|receiving)\s+(?:an?\s+|the\s+|new\s+|tv\s+|original\s+|television\s+)*anime\b"
    r"(?!\s+(?:trailer|visual|key|pv|teaser|cast|staff|theme|song|opening|ending|video|poster|image|illustration))"
    r"|\b(?:green-?lit|greenlights?|inspires?)\b.{0,30}\banime\b"
    r"|\badapted into (?:an? )?(?:tv )?anime\b"
    r"|\banime adaptation\b.{0,25}\b(?:announced|green-?lit|confirmed|revealed?)\b"
    r"|\b(?:announces?|announced|confirms?|unveils?|reveals?)\s+(?:an?\s+|new\s+|tv\s+)*anime\s+(?:adaptation|series|project)\b"
    r"|\btv anime\b.{0,15}\b(?:announced|green-?lit|confirmed)\b"
)
MANGA_ADAPT_RE = _c(
    r"\b(?:gets?|getting|receives?|launches|inspires?)\s+(?:an?\s+|the\s+|new\s+)*manga"
    r"(?:\s+(?:adaptation|spin-?off|sequel|series))?\b"
    r"|\bmanga adaptation\b.{0,25}\b(?:announced|launches|confirmed|revealed?)\b"
)
NOVEL_ADAPT_RE = _c(
    r"\b(?:gets?|getting|receives?|launches)\s+(?:an?\s+|the\s+|new\s+)*(?:light\s+)?novel"
    r"(?:\s+(?:adaptation|spin-?off|sequel|series))?\b"
    r"|\bnovel adaptation\b.{0,25}\b(?:announced|launches|confirmed|revealed?)\b"
)
CHAPTER_RE = _c(r"\bchapter\s*#?\d+\b|\bnew chapter\b")

MEDIA_THRESHOLD = 2.0
MEDIA_ORDER = ("Anime", "Manga", "Novel")


def _strip_non_media(text: str) -> str:
    """'Graphic novel' and 'visual novel' are not light novels."""
    return re.sub(r"\b(?:graphic|visual) novels?\b", " ", text or "", flags=re.I)


def _score(text: str, signals: list, weight: float = 1.0) -> float:
    return sum(w * weight for rx, w in signals if rx.search(text))


def classify_media(title: str, description: str = ""):
    """Score-based classification. Returns (media_type, {'Anime':x,'Manga':y,'Novel':z})."""
    t = _strip_non_media(title)
    d = _strip_non_media((description or "")[:600])

    scores = {
        "Anime": _score(t, ANIME_SIGNALS) + _score(d, ANIME_SIGNALS, 0.3),
        "Manga": _score(t, MANGA_SIGNALS) + _score(d, MANGA_SIGNALS, 0.3),
        "Novel": _score(t, NOVEL_SIGNALS) + _score(d, NOVEL_SIGNALS, 0.3),
    }

    anime_adapt = bool(ANIME_ADAPT_RE.search(t))
    chapter_event = bool(CHAPTER_RE.search(t))
    manga_adapt = (not anime_adapt) and bool(MANGA_ADAPT_RE.search(t))
    novel_adapt = (not anime_adapt) and bool(NOVEL_ADAPT_RE.search(t))

    if anime_adapt:
        scores["Anime"] += 5.0
    if chapter_event:
        scores["Manga"] += 5.0
    if manga_adapt:
        scores["Manga"] += 5.0
    if novel_adapt:
        scores["Novel"] += 5.0

    # "Light Novel X Anime Reveals Voice Cast": 'light novel' only names the source
    # material. If the headline carries anime signals and no manga/novel event,
    # the source-medium words are discounted so they cannot override the anime event.
    anime_in_title = _score(t, ANIME_SIGNALS) + (5.0 if anime_adapt else 0.0)
    if anime_in_title >= 3.0 and not (chapter_event or manga_adapt or novel_adapt):
        scores["Manga"] *= 0.5
        scores["Novel"] *= 0.5

    scores = {k: round(v, 2) for k, v in scores.items()}
    best = max(MEDIA_ORDER, key=lambda m: (scores[m], -MEDIA_ORDER.index(m)))
    return (best if scores[best] >= MEDIA_THRESHOLD else "Other"), scores


# =============================================================================
# TITLE / MEDIA NAME EXTRACTION
# =============================================================================

_VERBS = (
    r"gets?|getting|reveals?|unveils?|announces?|announced|confirms?|confirmed|launch(?:es)?|streams?|streaming|"
    r"streamed|premieres?|adds?|casts?|debuts?|ends?|concludes?|returns?|hits|sells|posts|previews?|delays?|"
    r"delayed|teases?|shares?|lists?|reports?|ships?|opens?|inspires?|wins?|releases?|released|receives?|"
    r"welcomes?|begins?|reaches|gains?|drops?|shows?|brings?|plans?|licenses?|licensed|acquires?|rescues?|sets|"
    r"green-?lit|greenlights?|reaffirms?|promises|celebrates?|unleashes|kicks|"
    r"to (?:get|stream|premiere|end|release|launch|air|debut|receive|return|add|conclude)"
)
BOUNDARY_RE = re.compile(r"\b(?:%s)\b" % _VERBS, re.I)
QUOTE_RE = re.compile(r"[\"“‘]([^\"”’]{2,80})[\"”’]|(?<![\w])'([^']{2,80})'(?![\w])")
LEADING_LABEL_RE = _c(r"^(?:interview|review|spoilers?|news|exclusive|breaking|update|rumou?r|report|poll|list|feature|editorial)\s*[:\-–|]\s*")
MARKER_RE = _c(
    r"\b(?:season\s*\d+|\d+(?:st|nd|rd|th)\s+season|(?:final|second|third|fourth)\s+season|part\s*\d+|cour\s*\d+|"
    r"chapter\s*#?\d+|episode\s*#?\d+|volume\s*\d+|vol\.?\s*\d+)\b.*$"
)
TRAIL_RE = _c(r"\s+(?:tv|original|anime|film|movie|manga|light novels?|web novels?|novels?|series|adaptation|sequel|project|ova|oad|live-action|television)\s*$")
LEAD_RE = _c(r"^(?:tv\s+anime|anime|manga|light novels?|web novels?|novels?|tv)\s+")
_NUM_WORDS = {"second": 2, "third": 3, "fourth": 4}
_STRIP_CHARS = " \t\"'“”‘’:-–—|,.;"


def find_season(text: str) -> str:
    m = re.search(
        r"\b(?:season\s*(\d+)|(\d+)(?:st|nd|rd|th)\s+season|(final|second|third|fourth)\s+season)\b", text, re.I
    )
    if not m:
        return ""
    if m.group(1) or m.group(2):
        return f"Season {m.group(1) or m.group(2)}"
    word = m.group(3).lower()
    return "Final Season" if word == "final" else f"Season {_NUM_WORDS[word]}"


def _strip_descriptors(s: str) -> str:
    s = s.strip(_STRIP_CHARS)
    s = MARKER_RE.sub("", s).strip(_STRIP_CHARS)
    for _ in range(6):
        new = TRAIL_RE.sub("", s).strip(_STRIP_CHARS)
        new = LEAD_RE.sub("", new).strip(_STRIP_CHARS)
        if new == s:
            break
        s = new
    return s


def extract_media_name(title: str):
    """Pull the work's actual name out of a news headline. Returns (name, season_label)."""
    t = (title or "").strip()
    season = find_season(t)

    qm = QUOTE_RE.search(t)
    if qm:
        cand = _strip_descriptors(next(g for g in qm.groups() if g))
        if cand:
            return shorten(cand, 70), season

    cut = t
    for m in BOUNDARY_RE.finditer(t):
        if m.start() > 0 and t[: m.start()].strip():
            cut = t[: m.start()]
            break
    cut = LEADING_LABEL_RE.sub("", cut)
    name = _strip_descriptors(cut)
    if not name:
        name = _strip_descriptors(re.split(r"[:\-–|,]", t)[0])
    return shorten(name or t, 70), season


# =============================================================================
# EVENT DETECTION
# =============================================================================


def _date_label(t, media):
    return "Premiere Date Announced" if media == "Anime" else "Release Date Announced"


def _ending_label(t, media):
    if re.search(r"\bto end\b|\bwill end\b|\bending (?:in|soon)\b|\bnearing\b", t, re.I):
        return "Ending Announced"
    return "Series Concludes"


def _episode_label(t, media):
    if re.search(r"\b(?:preview|synopsis|screenshots?|images?)\b", t, re.I):
        return "Episode Preview Revealed"
    return "New Episode Released"


def _delay_label(t, media):
    if re.search(r"hiatus|on break", t, re.I):
        return "Hiatus Announced"
    if re.search(r"cancel", t, re.I):
        return "Cancellation Announced"
    return "Delay Announced"


def _anime_adapt_label(t, media):
    return "TV Anime Adaptation Announced" if re.search(r"\btv anime\b", t, re.I) else "Anime Adaptation Announced"


# (key, dedup_key, regex, priority, label function). Lower priority number = more important.
EVENT_RULES = [
    ("anime_film", "anime_film",
     _c(r"\b(?:gets?|receives?|green-?lit|announces?|announced|confirm\w*)\b.{0,40}\banime (?:film|movie)\b"
        r"|\banime (?:film|movie)\b.{0,20}\b(?:announced|green-?lit|confirmed)\b"),
     1, lambda t, m: "Anime Film Announced"),
    ("adaptation_anime", "adaptation", ANIME_ADAPT_RE, 1, _anime_adapt_label),
    ("adaptation_manga", "adaptation", MANGA_ADAPT_RE, 1, lambda t, m: "Manga Adaptation Announced"),
    ("adaptation_novel", "adaptation", NOVEL_ADAPT_RE, 1, lambda t, m: "Novel Adaptation Announced"),
    ("new_season", "new_season",
     _c(r"\b(?:gets?|receives?|renewed|announces?|announced|confirm\w*|green-?lit)\b.{0,30}\b(?:season|sequel|cour)\b"
        r"|\b(?:season\s*\d+|\d+(?:st|nd|rd|th) season|final season|new season)\b.{0,25}\b(?:announced|confirmed|green-?lit|renewed)\b"),
     2, lambda t, m: "New Season Announced"),
    ("date", "date",
     _c(r"\b(?:premieres?|debuts?|launches|airs?|broadcasts?)\s+(?:in|on|this)\b|\b(?:premiere|broadcast|release|air|launch|debut)\s+(?:date|window|month)\b"
        r"|\bto premiere\b|\bpremiering\b|\bscheduled for\b"),
     2, _date_label),
    ("ending", "ending",
     _c(r"\b(?:concludes?|to end|will end|ends|wraps? up|finale|final (?:chapter|volume|episode|arc))\b"),
     2, _ending_label),
    ("chapter", "chapter", _c(r"\bchapter\s*#?\d+\b|\bnew chapter\b"), 3, lambda t, m: "New Chapter Released"),
    ("episode", "episode", _c(r"\bepisode\s*#?\d+\b|\bnew episode\b"), 3, _episode_label),
    ("license", "license", _c(r"\b(?:licenses?|licensed|acquires?)\b"), 3, lambda t, m: "License Announced"),
    ("streaming", "streaming", _c(r"\b(?:to stream|streams?|streaming|simulcast)\b"), 3, lambda t, m: "Streaming Announced"),
    ("volume", "volume",
     _c(r"\b(?:volume|vol\.?)\s*\d*\b.{0,30}\b(?:released?|ships?|out now|hits|arrives|sells)\b|\b(?:released?|ships?)\b.{0,20}\bvolume\b"),
     3, lambda t, m: "New Volume Released"),
    ("delay", "delay", _c(r"\b(?:delayed?|postponed?|hiatus|on break|cancel(?:s|led|ed)?)\b"), 3, _delay_label),
    ("interview", "interview", _c(r"\binterview\b"), 5, lambda t, m: "Interview Published"),
    ("sales", "sales", _c(r"\b(?:sales?|rankings?|box office|top \d+|sells|sold)\b"), 5, lambda t, m: "Rankings & Sales Update"),
]

REVEAL_PRIORITY = 4
# (key, regex, noun used in label, phrase used in context)
REVEAL_ITEMS = [
    ("trailer", _c(r"\btrailers?\b"), "Trailer", "a new trailer"),
    ("teaser", _c(r"\bteasers?\b"), "Teaser", "a teaser"),
    ("pv", _c(r"\bpv\b|\bpromo(?:tional)? video\b"), "Promo Video", "a promo video"),
    ("key_visual", _c(r"\b(?:key )?visuals?\b"), "Key Visual", "a key visual"),
    ("poster", _c(r"\bposter\b"), "Poster", "a poster"),
    ("staff", _c(r"\bstaff\b"), "Staff", "its staff"),
    ("cast", _c(r"\b(?:voice )?cast\b|\bvoice actors?\b|\bcasts\b"), "Cast", "its cast"),
    ("theme", _c(r"\b(?:opening|ending) (?:theme|song)s?\b|\btheme songs?\b"), "Theme Songs", "its theme songs"),
]


def _join_and(items: list) -> str:
    if len(items) > 1 and items[0].startswith("its "):  # "its main staff and first key visual"
        items = [items[0]] + [i[4:] if i.startswith("its ") else i for i in items[1:]]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def detect_events(text: str, media: str) -> list:
    """Return events sorted by importance (most important first)."""
    if not text:
        return []
    found = []
    for key, dedup, rx, prio, labeler in EVENT_RULES:
        m = rx.search(text)
        if m:
            found.append(Event(key, labeler(text, media), prio, (dedup,), m.start()))

    keys = {e.key for e in found}
    if "anime_film" in keys or "adaptation_anime" in keys:  # anime events trump manga/novel adaptation guesses
        found = [e for e in found if e.key not in ("adaptation_manga", "adaptation_novel")]
    if "ending" in keys:  # "Concludes With 12th Volume" is not a volume release
        found = [e for e in found if e.key != "volume"]

    items = []
    for key, rx, noun, phrase in REVEAL_ITEMS:
        m = rx.search(text)
        if not m:
            continue
        if key == "staff" and re.search(r"\bmain staff\b", text, re.I):
            noun, phrase = "Main Staff", "its main staff"
        if key == "key_visual" and re.search(r"\b(?:1st|first)\s+(?:key\s+)?visual", text, re.I):
            phrase = "its first key visual"
        items.append((m.start(), key, noun, phrase))
    if items:
        items.sort()
        nouns = [i[2] for i in items][:3]
        verb = "Announced" if [i[1] for i in items] == ["cast"] else "Revealed"
        label = (nouns[0] if len(nouns) == 1 else ", ".join(nouns[:-1]) + " & " + nouns[-1]) + " " + verb
        found.append(Event("reveal", label, REVEAL_PRIORITY, tuple(i[1] for i in items), items[0][0], [i[3] for i in items][:3]))

    found.sort(key=lambda e: (e.priority, e.pos))
    return found


def event_line(events: list) -> str:
    labels = []
    for e in events[:2]:
        if e.label not in labels and (not labels or e.priority <= REVEAL_PRIORITY):
            labels.append(e.label)
    return " • ".join(labels) if labels else "Latest Update"


# =============================================================================
# NUMBERS (chapter / episode / volume / season) - used to tell stories apart
# =============================================================================


def extract_numbers(title: str) -> dict:
    nums = {}
    for key, pat in (
        ("chapter", r"\bchapter\s*#?(\d+)"),
        ("episode", r"\bepisode\s*#?(\d+)|\bep\.?\s*#?(\d+)"),
        ("volume", r"\bvolume\s*#?(\d+)|\bvol\.?\s*#?(\d+)|\b(\d+)(?:st|nd|rd|th)\s+volume"),
    ):
        m = re.search(pat, title, re.I)
        if m:
            nums[key] = next(g for g in m.groups() if g)
    season = find_season(title)
    if season:
        nums["season"] = season.replace("Season ", "").lower()
    return nums


# =============================================================================
# CONTEXT GENERATION
# =============================================================================

DATE_RE = re.compile(
    r"\b((?:in|on|this)\s+(?:(?:early|mid|late)\s+)?(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
    r"(?:\s+\d{1,2}(?:st|nd|rd|th)?)?(?:,?\s+\d{4})?|(?:spring|summer|fall|autumn|winter)(?:\s+\d{4})?|\d{4}))\b",
    re.I,
)
JUNK_SENTENCE_RE = re.compile(
    r"click|subscribe|read more|sponsored|©|https?://|source:|follow us|newsletter|comments?\b|appeared first", re.I
)


def find_date_phrase(a: Article) -> str:
    for text in (a.title, a.description[:400]):
        m = DATE_RE.search(text or "")
        if m:
            return m.group(1).strip()
    return ""


def _tokens(s: str) -> set:
    return set(norm_text(s).split())


def _description_sentence(a: Article, used_text: str) -> str:
    """One informative, non-redundant sentence from the RSS description, or ''."""
    seen = _tokens(a.title + " " + used_text)
    for s in split_sentences(a.description)[:3]:
        if len(s) < 35 or JUNK_SENTENCE_RE.search(s):
            continue
        toks = _tokens(s)
        if not toks or len(toks & seen) / len(toks) >= 0.7:
            continue  # mostly repeats what we already say
        return shorten(s, 200)
    return ""


def _primary_sentence(a: Article, e: Event, name: str, subject: str, date_phrase: str) -> str:
    n = a.numbers
    low = a.title.lower()
    k = e.key
    anime = a.media == "Anime"
    if k == "adaptation_anime":
        thing = "a TV anime" if "tv anime" in low else "an anime adaptation"
        src = "light novel " if "light novel" in low else ("manga " if re.search(r"\bmanga\b", low) else "")
        return f"The {src}{name} is getting {thing}." if src else f"{name} is getting {thing}."
    if k == "anime_film":
        return f"{name} is getting an anime film."
    if k == "adaptation_manga":
        return f"{name} is getting a manga adaptation."
    if k == "adaptation_novel":
        return f"{name} is getting a novel adaptation."
    if k == "new_season":
        return f"{subject} has been announced." if a.season else f"A new season of {name} has been announced."
    if k == "date":
        if date_phrase:
            return f"{subject} premieres {date_phrase}." if anime else f"{subject} is scheduled for release {date_phrase}."
        return f"A {'premiere' if anime else 'release'} date has been announced for {subject}."
    if k == "ending":
        if e.label == "Ending Announced":
            return f"{name} is coming to an end."
        return f"{name} concludes with volume {n['volume']}." if n.get("volume") else f"{name} has concluded."
    if k == "chapter":
        if n.get("chapter"):
            return f"{name} Chapter {n['chapter']} has been released, continuing the current manga storyline."
        return f"A new chapter of {name} has been released."
    if k == "episode":
        ep = f" Episode {n['episode']}" if n.get("episode") else ""
        if e.label == "Episode Preview Revealed":
            return f"A preview for {subject}{ep} has been revealed."
        return f"{subject}{ep} has been released." if ep else f"A new episode of {subject} has been released."
    if k == "license":
        return f"A new license has been announced for {name}."
    if k == "streaming":
        return f"Streaming details have been announced for {subject}."
    if k == "volume":
        return f"Volume {n['volume']} of {name} has been released." if n.get("volume") else f"A new volume of {name} has been released."
    if k == "delay":
        return {"Hiatus Announced": f"{name} is going on hiatus.",
                "Cancellation Announced": f"{name} has been cancelled."}.get(e.label, f"{name} has been delayed.")
    if k == "reveal":
        verb = "announced" if e.dedup == ("cast",) else "revealed"
        return f"{subject} has {verb} {_join_and(e.phrases)}."
    return ""


def _secondary_sentence(a: Article, e: Event, date_phrase: str) -> str:
    if e.key == "reveal":
        return f"The latest announcement also reveals {_join_and(e.phrases)}."
    if e.key == "date":
        if date_phrase and a.media == "Anime":
            return f"It premieres {date_phrase}."
        return f"A {'premiere' if a.media == 'Anime' else 'release'} date has also been announced."
    if e.key == "new_season":
        return "A new season has also been announced."
    return ""


def build_context(a: Article) -> str:
    """1-3 short factual sentences derived from the headline/description."""
    name = a.media_name or shorten(a.title, 80)
    subject = f"{name} {a.season}".strip() if a.season else name
    date_phrase = find_date_phrase(a)
    sentences = []
    if a.events:
        first = _primary_sentence(a, a.events[0], name, subject, date_phrase)
        if first:
            sentences.append(first)
        for e in a.events[1:2]:
            second = _secondary_sentence(a, e, date_phrase)
            if second:
                sentences.append(second)
    if len(sentences) < 3:
        extra = _description_sentence(a, " ".join(sentences))
        if extra and (len(sentences) < 2 or len(" ".join(sentences)) < 160):
            sentences.append(extra)
    if not sentences:
        sentences.append(shorten(a.title, 220))
    return shorten(" ".join(sentences), 420)


# =============================================================================
# SMART DUPLICATE DETECTION
# =============================================================================

_NOISE_WORDS = (
    "the a an and for in on with its it is are was to of at by from as gets get getting receives receive reveals "
    "reveal revealed unveils unveil unveiled announces announce announced confirms confirmed launches launch "
    "released releases shows shares debuts new tv anime manga light novel novels adaptation series season part "
    "cour chapter episode volume vol official officially first second final main"
).split()


def norm_text(s: str) -> str:
    """Lower-case, strip accents/quotes/punctuation, unify common romanization variants."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = re.sub(r"['’`´]s\b", "", s)
    s = re.sub(r"['’`´]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    toks = []
    for t in s.split():
        if t == "wo":
            t = "o"
        toks.append(t.replace("ou", "o").replace("oo", "o").replace("uu", "u"))
    return " ".join(toks)


NOISE = {norm_text(w) for w in _NOISE_WORDS}


def norm_entity(name: str) -> str:
    return " ".join(t for t in norm_text(name).split() if t not in ("the", "a", "an"))


def title_tokens(title: str, numbers: dict) -> list:
    drop = {str(v).lower() for v in numbers.values()}
    out = []
    for t in norm_text(title).split():
        if t in NOISE or t in drop or re.fullmatch(r"\d+(?:st|nd|rd|th)", t):
            continue
        out.append(t)
    return out


def make_signature(a: Article) -> dict:
    return {
        "title_norm": norm_text(a.title),
        "entity": norm_entity(a.media_name),
        "tokens": title_tokens(a.title, a.numbers),
        "events": sorted({k for e in a.events for k in e.dedup}),
        "numbers": a.numbers,
        "source": a.source,
        "link": a.link,
        "guid": a.guid,
    }


def entity_similarity(ea: str, eb: str) -> float:
    if not ea or not eb:
        return 0.0
    if ea == eb:
        return 1.0
    ratio = difflib.SequenceMatcher(None, ea, eb).ratio()
    ta, tb = set(ea.split()), set(eb.split())
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if small and small <= big and (len(small) >= 2 or len(" ".join(small)) >= 5):
        ratio = max(ratio, 0.92)  # one name is contained in the other (subtitle differences)
    return ratio


def token_similarity(ta: list, tb: list) -> float:
    sa, sb = set(ta), set(tb)
    if not sa or not sb:
        return 0.0
    jac = len(sa & sb) / len(sa | sb)
    seq = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    return 0.5 * jac + 0.5 * seq


def event_similarity(ea: set, eb: set) -> float:
    if not ea and not eb:
        return 0.6  # nothing to compare: neutral
    if not ea or not eb:
        return 0.3
    return len(ea & eb) / len(ea | eb)


def same_story(a: dict, b: dict):
    """Compare two signatures. Returns (is_same_story, score)."""
    la, lb = link_key(a.get("link")), link_key(b.get("link"))
    if la and la == lb:
        return True, 1.0
    if a.get("guid") and a.get("guid") == b.get("guid"):
        return True, 1.0
    na, nb = a.get("title_norm", ""), b.get("title_norm", "")
    if na and na == nb:
        return True, 1.0

    e = entity_similarity(a.get("entity", ""), b.get("entity", ""))
    t = token_similarity(a.get("tokens", []), b.get("tokens", []))
    eva, evb = set(a.get("events", [])), set(b.get("events", []))
    ev = event_similarity(eva, evb)
    score = round((0.5 * e + 0.3 * t + 0.2 * ev) if e > 0 else (0.8 * t + 0.2 * ev), 3)

    # Different chapter/episode/season/volume number = different story.
    numa, numb = a.get("numbers", {}), b.get("numbers", {})
    if any(numa[k] != numb[k] for k in set(numa) & set(numb)):
        return False, score

    same_src = a.get("source") == b.get("source")
    if score >= (SAME_SOURCE_SCORE if same_src else SAME_STORY_SCORE):
        return True, score
    if not same_src and score >= CONTEXT_SCORE and e >= 0.9 and (eva & evb):
        return True, score
    return False, score


def cluster_articles(articles: list) -> list:
    """Group same-story articles (transitively). Each cluster's first item is the best representative."""
    n = len(articles)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if same_story(articles[i].sig, articles[j].sig)[0]:
                parent[find(i)] = find(j)

    groups = {}
    for i, art in enumerate(articles):
        groups.setdefault(find(i), []).append(art)
    clusters = []
    for members in groups.values():
        members.sort(key=lambda x: (x.source_rank, not x.image, -len(x.description)))
        clusters.append(members)
    return clusters


# =============================================================================
# ANALYSIS PIPELINE
# =============================================================================


def analyze(a: Article) -> Article:
    a.media, a.scores = classify_media(a.title, a.description)
    a.media_name, a.season = extract_media_name(a.title)
    a.numbers = extract_numbers(a.title)
    a.events = detect_events(a.title, a.media) or detect_events(a.description[:300], a.media)
    a.context = build_context(a)
    a.sig = make_signature(a)
    return a


def analyze_all(articles: list) -> list:
    out = []
    for a in articles:
        try:
            out.append(analyze(a))
        except Exception as e:  # noqa: BLE001
            log.warning("could not analyze '%s': %s", shorten(a.title, 50), e.__class__.__name__)
    return out


# =============================================================================
# POSTED STATE (posted.json)
# =============================================================================


class State:
    """Persistent memory of posted articles."""

    def __init__(self, path: str = POSTED_FILE):
        self.path = path
        self.entries: list = []
        self.first_run = True
        self._links: set = set()
        self._guids: set = set()
        self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            backup = self.path + ".corrupt"
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            log.warning("posted.json unreadable (%s) - moved to %s, re-seeding to avoid flooding", e.__class__.__name__, backup)
            return

        seeded = None
        if isinstance(data, dict):
            raw = data.get("entries") or data.get("posted") or []
            seeded = data.get("seeded")
        elif isinstance(data, list):
            raw = data
        else:
            raw = []
        for item in raw:
            entry = self._coerce(item)
            if entry:
                self.entries.append(entry)
        self.first_run = not (seeded if seeded is not None else bool(self.entries))
        self._reindex()

    @staticmethod
    def _coerce(item):
        """Accept entries from older/simpler formats (plain URL / GUID strings)."""
        if isinstance(item, str):
            item = item.strip()
            if not item:
                return None
            return {"link": item, "guid": "", "ts": 0.0} if item.startswith("http") else {"link": "", "guid": item, "ts": 0.0}
        if isinstance(item, dict):
            item.setdefault("ts", 0.0)
            return item
        return None

    def _reindex(self) -> None:
        self._links = {link_key(e.get("link")) for e in self.entries if e.get("link")}
        self._guids = {e["guid"] for e in self.entries if e.get("guid")}

    def prune(self) -> None:
        cutoff = time.time() - STATE_RETENTION_DAYS * 86400
        kept = [e for e in self.entries if not e.get("ts") or e["ts"] >= cutoff]
        self.entries = kept[-STATE_MAX_ENTRIES:]
        self._reindex()

    def save(self) -> None:
        self.prune()
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 2, "seeded": True, "entries": self.entries}, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)
            self.first_run = False
        except OSError as e:
            log.error("could not save posted.json: %s", e)

    # -- API -------------------------------------------------------------------
    def already_posted(self, sig: dict) -> bool:
        if link_key(sig.get("link")) in self._links or (sig.get("guid") and sig["guid"] in self._guids):
            return True
        cutoff = time.time() - DUPLICATE_WINDOW_DAYS * 86400
        for e in self.entries:
            if e.get("title_norm") and e.get("ts", 0) >= cutoff and same_story(sig, e)[0]:
                return True
        return False

    def remember_entry(self, a: Article, seeded: bool = False) -> None:
        entry = dict(a.sig)
        entry.update({"media": a.media, "name": a.media_name, "ts": time.time(), "seeded": seeded})
        self.entries.append(entry)
        if entry.get("link"):
            self._links.add(link_key(entry["link"]))
        if entry.get("guid"):
            self._guids.add(entry["guid"])


# =============================================================================
# DISCORD EMBEDS & WEBHOOK
# =============================================================================

COLOR_PALETTE = [
    # purple / violet
    0x8E44AD, 0x9B59B6, 0x7D3C98, 0xA569BD, 0x6C3483, 0x8A2BE2, 0x9370DB, 0x7B68EE, 0xB57EDC,
    # magenta / pink
    0xD81B60, 0xC2185B, 0xE91E8C, 0xBA2D9C, 0xFF69B4, 0xF06292, 0xFF8FB1, 0xEC7FA9,
    # blue / cyan / teal
    0x3498DB, 0x2E86DE, 0x1E90FF, 0x4A90E2, 0x5DADE2, 0x00BCD4, 0x26C6DA, 0x00ACC1, 0x87CEEB,
    0x1ABC9C, 0x16A085, 0x009688, 0x26A69A, 0x3EB489,
    # green / lime
    0x2ECC71, 0x27AE60, 0x43A047, 0x66BB6A, 0x9ACD32, 0xAFD835, 0xC0CA33, 0xB5E61D,
    # yellow / gold / orange
    0xF1C40F, 0xF9E04B, 0xFFC107, 0xE6B800, 0xD4AF37, 0xE67E22, 0xFF9800, 0xFB8C00, 0xF39C12,
    # red / crimson / coral
    0xE74C3C, 0xF44336, 0xD32F2F, 0xFF5252, 0xDC143C, 0xB71C1C, 0xC62828, 0xFF7F50, 0xFF6F61, 0xFA8072,
    # indigo
    0x3F51B5, 0x5C6BC0, 0x303F9F, 0x4B6CC1,
]
_last_color = None


def random_color() -> int:
    global _last_color
    color = random.choice([c for c in COLOR_PALETTE if c != _last_color])
    _last_color = color
    return color


MEDIA_LABELS = {"Anime": "ANIME", "Manga": "MANGA", "Novel": "LIGHT NOVEL", "Other": "NEWS"}


def build_embed(a: Article) -> dict:
    name = a.media_name or shorten(a.title, 120)
    embed = {
        "author": {"name": MEDIA_LABELS.get(a.media, "NEWS")},
        "title": shorten(name, 250),
        "url": a.link,
        "description": f"{shorten(a.context, 700)}\n\n[Read Full Article]({a.link})",
        "color": random_color(),
        "fields": [
            {"name": "Event", "value": shorten(event_line(a.events), 1000), "inline": False},
            {"name": "Type", "value": a.media if a.media != "Other" else "General News", "inline": True},
            {"name": "Source", "value": shorten(a.source, 200), "inline": True},
        ],
        "footer": {"text": a.source},
    }
    if a.image:
        embed["image"] = {"url": a.image}
    if a.published and a.published <= time.time() + 300:
        embed["timestamp"] = datetime.fromtimestamp(a.published, tz=timezone.utc).isoformat()
    return embed


class WebhookInvalid(Exception):
    """Webhook is missing/deleted/unauthorized - retrying is pointless."""


def _retry_after(resp) -> float:
    try:
        return float(resp.json().get("retry_after"))
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        return float(resp.headers.get("Retry-After", 2))
    except ValueError:
        return 2.0


def send_webhook(embed: dict, mention_role: bool = False, retries: int = 3) -> bool:
    """POST one embed. Handles rate limits, transient errors and invalid webhooks."""
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}  # never @everyone / @here
    if mention_role and DISCORD_ROLE_ID.isdigit():
        payload["content"] = f"<@&{DISCORD_ROLE_ID}>"
        payload["allowed_mentions"] = {"parse": [], "roles": [DISCORD_ROLE_ID]}

    for attempt in range(retries + 1):
        try:
            r = SESSION.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            log.warning("Discord network error (%s), attempt %d", e.__class__.__name__, attempt + 1)
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (200, 204):
            return True
        if r.status_code == 429:
            wait = _retry_after(r)
            log.warning("Discord rate limit: waiting %.1fs", wait)
            if wait > 60:
                return False
            time.sleep(wait + 0.5)
            continue
        if r.status_code in (401, 403, 404):
            raise WebhookInvalid(f"HTTP {r.status_code} - the webhook is invalid or was deleted. Create a new one and update .env")
        if r.status_code >= 500:
            time.sleep(2 * (attempt + 1))
            continue
        log.warning("Discord rejected the message (HTTP %s): %s", r.status_code, redact(r.text[:200]))
        return False
    return False


# =============================================================================
# CYCLE
# =============================================================================


def rank_score(a: Article) -> float:
    prio = min((e.priority for e in a.events), default=6)
    score = 100.0 - prio * 10
    if a.media == "Other":
        score -= 15
    if a.image:
        score += 5
    score -= a.source_rank * 1.5
    if a.published:
        age_h = max(0.0, (time.time() - a.published) / 3600)
        score += max(0.0, 10 - age_h * 10 / max(MAX_ARTICLE_AGE_HOURS, 1))
    return score


def is_fresh(a: Article) -> bool:
    if not a.published or MAX_ARTICLE_AGE_HOURS <= 0:
        return True
    return (time.time() - a.published) <= MAX_ARTICLE_AGE_HOURS * 3600


def banner(text: str) -> None:
    print("=" * 40)
    print(f" {text}")
    print("=" * 40)


def run_cycle(state: State, dry_run: bool = False) -> None:
    banner("ANIME NEWS → DISCORD")
    articles, status = collect_articles(RSS_FEEDS)
    ok_feeds = sum(1 for _, ok, _ in status if ok)
    print(f"\nFeeds: {len(RSS_FEEDS)} ({ok_feeds} reachable)")
    for name, ok, note in status:
        print(f"  {'✓' if ok else '✗'} {name}: {note}")

    articles = analyze_all(articles)
    print(f"Collected: {len(articles)}")

    # --- first run: seed state, do not flood the channel ----------------------
    if state.first_run and not dry_run:
        articles.sort(key=lambda x: x.published, reverse=True)
        keep = articles[:FIRST_RUN_POST_LATEST] if FIRST_RUN_POST_LATEST > 0 else []
        keep_ids = {id(x) for x in keep}
        for a in articles:
            if id(a) not in keep_ids:
                state.remember_entry(a, seeded=True)
        state.save()
        print(f"\nFirst run: remembered {len(articles) - len(keep)} existing articles without posting.")
        if not keep:
            print("Only new articles from now on will be posted.\nCycle complete.\n")
            return
        articles = keep

    # --- filter posted / stale -----------------------------------------------
    fresh = [a for a in articles if is_fresh(a)]
    new = [a for a in fresh if not state.already_posted(a.sig)]
    already = len(fresh) - len(new)

    clusters = cluster_articles(new)
    reps = [c[0] for c in clusters]
    cluster_of = {id(c[0]): c for c in clusters}
    dupes = len(new) - len(reps)

    reps.sort(key=rank_score, reverse=True)
    selected, seen_entities = [], set()
    for a in reps:  # at most one story per franchise per cycle
        ent = a.sig.get("entity")
        if ent and ent in seen_entities:
            continue
        selected.append(a)
        if ent:
            seen_entities.add(ent)
        if len(selected) >= MAX_POSTS_PER_CYCLE:
            break

    print(f"New candidates: {len(new)}")
    print(f"Already posted/old: {already + len(articles) - len(fresh)}")
    print(f"Duplicates removed: {dupes}")
    print(f"Posts allowed: {min(len(selected), MAX_POSTS_PER_CYCLE)}\n")

    if not selected:
        print("Nothing new to post.\nCycle complete.\n")
        return

    print("Posting:" if not dry_run else "Dry run (nothing is sent or saved):")
    sent = 0
    for a in selected:
        a.image = resolve_image(a)
        embed = build_embed(a)
        if dry_run:
            print(f"- [{a.media}] {a.media_name} | {event_line(a.events)} | {a.source}")
            print(f"    {a.context}")
            print(f"    image: {a.image or '-'}")
            continue
        try:
            ok = send_webhook(embed, mention_role=(sent == 0))
            if not ok and a.image:  # maybe the image broke it: retry without
                embed.pop("image", None)
                ok = send_webhook(embed, mention_role=(sent == 0))
        except WebhookInvalid as e:
            print(f"✗ {redact(e)}")
            log.error("Stopping cycle: %s", redact(e))
            break
        if ok:
            sent += 1
            for member in cluster_of[id(a)]:  # also remember the other sources' versions
                state.remember_entry(member)
            state.save()
            print(f"✓ {a.media_name}")
            time.sleep(POST_DELAY_SECONDS)
        else:
            print(f"✗ {a.media_name} (will retry next cycle)")
    print("\nCycle complete.\n")


# =============================================================================
# TEST MODES
# =============================================================================

MEDIA_TEST_CASES = [
    ("The Apothecary Diaries Season 3 Reveals New Trailer", "Anime"),
    ("One Piece Chapter 1170 Released", "Manga"),
    ("Some Light Novel Gets TV Anime", "Anime"),
    ("Rebuild World TV Anime Reveals Main Staff", "Anime"),
    ("Manga X Gets TV Anime Adaptation", "Anime"),
    ("Frieren Season 2 Reveals Trailer", "Anime"),
    ("Graphic Novel Gets Hardcover Release", "Other"),
    ("Light Novel X Anime Reveals Voice Cast", "Anime"),
    ("Light Novel X Concludes With 12th Volume", "Novel"),
    ("Anime X Gets Manga Adaptation", "Manga"),
]

DUPLICATE_TEST_CASES = [
    ("Anime News Network", "Rebuild World TV Anime Reveals Main Staff, 1st Key Visual",
     "Crunchyroll News", "Rebuild World Anime Unveils Main Staff, Key Visual", True),
    ("Anime News Network", "Rebuild World TV Anime Reveals Main Staff, 1st Key Visual",
     "MyAnimeList", "'Rebuild World' Reveals Main Staff", True),
    ("Anime Corner", "Delta to Gamma no Rigakubu Note Anime Adaptation Announced",
     "Anime News Network", "Light Novel 'Delta to Gamma no Rigakubu Note' Gets TV Anime", True),
    ("Anime News Network", "Tsukimichi Moonlit Fantasy Season 3 Reveals Trailer",
     "MyAnimeList", "Tsukimichi -Moonlit Fantasy- Season 3 Unveils Trailer", True),
    ("Anime News Network", "Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e Anime Reveals Trailer",
     "Crunchyroll News", "Yokoso Jitsuryoku Shijo Shugi no Kyoshitsu e Anime Unveils Trailer", True),
    ("Anime News Network", "Frieren Season 2 Reveals Trailer",
     "Crunchyroll News", "Frieren Season 2 Reveals Main Staff", False),
    ("Anime News Network", "Rebuild World TV Anime Reveals Main Staff, 1st Key Visual",
     "Crunchyroll News", "Rebuild World Anime Unveils Trailer", False),
    ("Anime News Network", "One Piece Chapter 1170 Released",
     "MyAnimeList", "One Piece Chapter 1171 Released", False),
    ("Anime News Network", "Frieren Season 2 Reveals Trailer",
     "MyAnimeList", "Spy x Family Season 4 Reveals Trailer", False),
]


def _mk(title: str, source: str = "Test", desc: str = "", rank: int = 0) -> Article:
    return analyze(Article(title=title, link=f"https://example.com/{abs(hash((title, source)))}", source=source,
                           description=desc, source_rank=rank))


def test_media() -> int:
    banner("MEDIA DETECTION TEST")
    print()
    failures = 0
    for title, expected in MEDIA_TEST_CASES:
        media, sc = classify_media(title)
        ok = media == expected
        failures += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {media:<6} (expected {expected:<6}) "
              f"A={sc['Anime']:<5} M={sc['Manga']:<5} N={sc['Novel']:<5} | {title}")
    print(f"\n{len(MEDIA_TEST_CASES) - failures}/{len(MEDIA_TEST_CASES)} known cases passed.\n")

    print("REAL FEED ARTICLES\n")
    articles, status = collect_articles(RSS_FEEDS)
    articles = analyze_all(articles)
    if not articles:
        print("(no feed articles available: " + "; ".join(f"{n}: {note}" for n, ok, note in status if not ok) + ")")
    for name in dict.fromkeys(a.source for a in articles):
        print(f"[{name}]")
        for a in [x for x in articles if x.source == name][:12]:
            print(f"{a.media:<6} | {shorten(a.media_name, 28):<28} | {shorten(a.title, 80)}")
        print()
    return 1 if failures else 0


def test_duplicates() -> int:
    banner("SMART DUPLICATE DETECTION TEST")
    print("\nKNOWN CASES\n")
    failures = 0
    for src_a, title_a, src_b, title_b, expected in DUPLICATE_TEST_CASES:
        a, b = _mk(title_a, src_a), _mk(title_b, src_b)
        same, score = same_story(a.sig, b.sig)
        ok = same == expected
        failures += not ok
        print(f"Score: {score:.2f}\n\n{src_a}:\n{title_a}\n\n{src_b}:\n{title_b}\n")
        print(f"SAME STORY: {same}  (expected {expected})  {'OK' if ok else 'FAIL'}\n" + "-" * 40 + "\n")
    print(f"{len(DUPLICATE_TEST_CASES) - failures}/{len(DUPLICATE_TEST_CASES)} known cases passed.\n")

    print("REAL FEED ARTICLES\n")
    articles, status = collect_articles(RSS_FEEDS)
    articles = analyze_all(articles)
    print(f"Collected {len(articles)} articles.\n")
    pairs = []
    for i in range(len(articles)):
        for j in range(i + 1, len(articles)):
            if articles[i].source == articles[j].source:
                continue
            same, score = same_story(articles[i].sig, articles[j].sig)
            if same or score >= 0.55:
                pairs.append((score, same, articles[i], articles[j]))
    pairs.sort(key=lambda p: p[0], reverse=True)
    if not pairs:
        print("No related cross-source pairs found in the current feeds.")
    for score, same, a, b in pairs[:15]:
        print(f"Score: {score:.2f}\n\n{a.source}:\n{a.title}\n\n{b.source}:\n{b.title}\n\nSAME STORY: {same}\n")
    return 1 if failures else 0


def test_basic(send: bool) -> int:
    banner("BASIC TEST")
    print()
    problems = 0
    if DISCORD_WEBHOOK_URL:
        valid = bool(WEBHOOK_RE.match(DISCORD_WEBHOOK_URL))
        print(f"{'OK  ' if valid else 'WARN'} DISCORD_WEBHOOK_URL is set ({'format looks valid' if valid else 'format looks wrong'})")
        problems += not valid
    else:
        print("WARN DISCORD_WEBHOOK_URL is not set (copy .env.example to .env)")
        problems += 1
    print(f"OK   DISCORD_ROLE_ID: {'set' if DISCORD_ROLE_ID.isdigit() else 'not set (no role mentions)'}")
    st = State()
    print(f"OK   posted.json: {len(st.entries)} entries, first run = {st.first_run}")

    articles, status = collect_articles(RSS_FEEDS)
    for name, ok, note in status:
        print(f"{'OK  ' if ok else 'FAIL'} feed {name}: {note}")
        problems += not ok
    articles = analyze_all(articles)
    sample = articles[0] if articles else _mk("Rebuild World TV Anime Reveals Main Staff, 1st Key Visual", "Sample Source",
                                              "Sample description text for the preview.")
    sample.image = sample.image if articles else ""
    print("\nSample embed (NOT sent unless --send):\n")
    print(json.dumps(build_embed(sample), indent=2, ensure_ascii=False))
    if send:
        if not WEBHOOK_RE.match(DISCORD_WEBHOOK_URL or ""):
            print("\nCannot send: webhook URL missing/invalid.")
            return 1
        try:
            ok = send_webhook(build_embed(sample))
        except WebhookInvalid as e:
            print(f"\nSend failed: {redact(e)}")
            return 1
        print(f"\nSample {'sent ✓' if ok else 'FAILED ✗'}")
        problems += not ok
    return 1 if problems else 0


# =============================================================================
# ENTRY POINT
# =============================================================================


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    p = argparse.ArgumentParser(description="Anime/Manga/Novel news -> Discord webhook")
    p.add_argument("--test", action="store_true", help="basic config/feed test (never posts unless --send)")
    p.add_argument("--send", action="store_true", help="with --test: send one sample embed")
    p.add_argument("--test-media", action="store_true", help="media classification test")
    p.add_argument("--test-duplicates", action="store_true", help="duplicate detection test")
    p.add_argument("--once", action="store_true", help="run one cycle and exit")
    p.add_argument("--dry-run", action="store_true", help="run one cycle, print embeds, post/save nothing")
    args = p.parse_args()

    if args.test_media:
        return test_media()
    if args.test_duplicates:
        return test_duplicates()
    if args.test:
        return test_basic(args.send)

    if not args.dry_run and not WEBHOOK_RE.match(DISCORD_WEBHOOK_URL):
        print("ERROR: DISCORD_WEBHOOK_URL is missing or not a valid Discord webhook URL.\n"
              "Copy .env.example to .env and paste your webhook URL there.")
        return 1

    state = State()
    if args.dry_run:
        run_cycle(state, dry_run=True)
        return 0

    try:
        while True:
            try:
                run_cycle(state)
            except Exception as e:  # noqa: BLE001 - keep the service alive
                log.error("cycle failed: %s: %s", e.__class__.__name__, redact(e))
            if args.once:
                return 0
            print(f"Next check in {CHECK_INTERVAL_MINUTES} minutes.\n")
            time.sleep(CHECK_INTERVAL_MINUTES * 60)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())