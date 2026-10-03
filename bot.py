import argparse
import colorsys
import difflib
import html
import json
import os
import random
import re
import time
from datetime import datetime, timezone

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


# ============================================================
# Anime News → Discord
# RSS feeds -> compact randomized Discord embeds
# ============================================================

load_dotenv()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

FEEDS_FILE = "feeds.json"
STATE_FILE = "posted.json"

# Check every 15 minutes
CHECK_INTERVAL_SECONDS = 15 * 60

# Maximum number of NEW articles posted during one cycle
MAX_POSTS_PER_CYCLE = 3

# Number of recent RSS entries to inspect from each feed
RSS_ITEMS_TO_SCAN = 12

# First normal run seeds existing articles instead of flooding Discord.
SEED_EXISTING_ON_FIRST_RUN = True

# Optional Discord role mention.
# Leave blank to disable.
MENTION_ROLE_ID = os.getenv("DISCORD_ROLE_ID", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36 AnimeNewsDiscord/1.0"
)

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


# ============================================================
# LARGE RANDOM COLOR PALETTE
# ============================================================

def build_color_palette():
    """
    Generate hundreds of visually usable Discord colors.

    120 hue positions × 4 saturation levels × 3 brightness levels
    = 1440 possible colors.
    """

    palette = []

    for hue in range(0, 360, 3):
        for saturation in (0.72, 0.82, 0.92, 1.00):
            for value in (0.72, 0.84, 0.96):

                r, g, b = colorsys.hsv_to_rgb(
                    hue / 360,
                    saturation,
                    value,
                )

                rgb = (
                    (int(r * 255) << 16)
                    | (int(g * 255) << 8)
                    | int(b * 255)
                )

                palette.append(rgb)

    return palette


EMBED_COLORS = build_color_palette()

last_color = None


def random_embed_color():
    """
    Return a random color while preventing
    the exact same color from appearing twice consecutively.
    """

    global last_color

    choices = [
        color
        for color in EMBED_COLORS
        if color != last_color
    ]

    color = random.choice(choices)

    last_color = color

    return color


# ============================================================
# FILES / STATE
# ============================================================

def load_feeds():
    with open(FEEDS_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def load_state():

    if not os.path.exists(STATE_FILE):
        return {
            "initialized": False,
            "posted": {},
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            state = json.load(file)

        # Backwards compatibility with old simple posted.json
        if isinstance(state, list):

            return {
                "initialized": True,
                "posted": {
                    str(item): {
                        "title": "",
                        "source": "",
                    }
                    for item in state
                },
            }

        state.setdefault(
            "initialized",
            False
        )

        state.setdefault(
            "posted",
            {}
        )

        return state

    except (
        json.JSONDecodeError,
        OSError
    ):

        return {
            "initialized": False,
            "posted": {},
        }


def save_state(state):

    # Keep the state file from becoming huge.
    posted_items = list(
        state.get("posted", {}).items()
    )[-1000:]

    state["posted"] = dict(posted_items)

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            state,
            file,
            indent=2,
            ensure_ascii=False
        )


# ============================================================
# TEXT CLEANING
# ============================================================

def clean_html_text(value):

    if not value:
        return ""

    soup = BeautifulSoup(
        value,
        "html.parser"
    )

    # Remove scripts and styles
    for tag in soup(
        ["script", "style"]
    ):
        tag.decompose()

    text = soup.get_text(
        " ",
        strip=True
    )

    # Decode:
    # &hellip;
    # &amp;
    # &nbsp;
    # etc.
    text = html.unescape(text)

    # Normalize whitespace
    text = re.sub(
        r"\s+",
        " ",
        text
    )

    # Remove common RSS leftovers
    text = text.replace(
        "Read more",
        ""
    ).strip()

    return text


def shorten(text, limit=280):

    text = clean_html_text(text)

    if len(text) <= limit:
        return text

    candidate = text[:limit]

    # Prefer sentence boundary
    last_stop = max(
        candidate.rfind(". "),
        candidate.rfind("! "),
        candidate.rfind("? "),
    )

    if last_stop >= int(
        limit * 0.55
    ):

        return candidate[
            :last_stop + 1
        ]

    return (
        candidate
        .rsplit(" ", 1)[0]
        .rstrip(".,;:")
        + "…"
    )


# ============================================================
# IMAGE EXTRACTION
# ============================================================

def get_entry_image(entry):

    candidates = []

    # RSS media formats
    for key in (
        "media_content",
        "media_thumbnail",
    ):

        values = entry.get(
            key,
            []
        )

        if isinstance(
            values,
            dict
        ):
            values = [values]

        candidates.extend(
            values or []
        )

    # RSS enclosure
    enclosure = entry.get(
        "enclosures",
        []
    )

    if enclosure:
        candidates.extend(
            enclosure
        )

    # Find image URL
    for item in candidates:

        if not isinstance(
            item,
            dict
        ):
            continue

        url = (
            item.get("url")
            or item.get("href")
        )

        mime = (
            item.get("type")
            or ""
        ).lower()

        if url and (
            mime.startswith("image/")
            or not mime
        ):

            return url

    # Some feeds put image inside HTML
    raw_html = (
        entry.get(
            "content",
            [{}]
        )[0].get(
            "value",
            ""
        )
        if entry.get("content")
        else entry.get(
            "summary",
            ""
        )
    )

    match = re.search(
        r'<img[^>]+src=["\']([^"\']+)["\']',
        raw_html,
        re.I
    )

    if match:

        return html.unescape(
            match.group(1)
        )

    return None


def get_og_image(article_url):

    """
    Fallback image extraction.

    Some websites block automated requests,
    so failure here is completely normal.
    """

    if not article_url:
        return None

    try:

        response = session.get(
            article_url,
            timeout=8,
            allow_redirects=True
        )

        if response.status_code != 200:
            return None

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        for attrs in (
            {"property": "og:image"},
            {"name": "twitter:image"},
        ):

            tag = soup.find(
                "meta",
                attrs=attrs
            )

            if (
                tag
                and tag.get("content")
            ):

                return tag["content"]

    except requests.RequestException:
        pass

    return None


# ============================================================
# MEDIA TYPE CLASSIFICATION
# ============================================================

def classify_type(
    title,
    description
):

    text = (
        f"{title} {description}"
        .lower()
    )

    # Strong novel signal
    if re.search(
        r"\blight novel\b|\bnovel\b",
        text
    ):

        return "Novel"

    # Strong manga signal
    if re.search(
        r"\bmanga\b|"
        r"\bmanhwa\b|"
        r"\bwebtoon\b|"
        r"\bone[- ]shot\b",
        text
    ):

        return "Manga"

    # Anime signal
    if re.search(
        r"\banime\b|"
        r"\bseason\b|"
        r"\bcour\b|"
        r"\bepisode\b|"
        r"\btrailer\b|"
        r"\bopening\b|"
        r"\bending\b|"
        r"\bvoice cast\b",
        text
    ):

        return "Anime"

    # Default
    return "Anime"


# ============================================================
# NEWS TOPIC CLASSIFICATION
# ============================================================

TOPIC_RULES = [

    (
        "New Chapter",
        [
            r"\bchapter\b",
            r"\bchapters\b",
            r"\bone[- ]shot\b",
        ],
    ),

    (
        "New Episode",
        [
            r"\bepisode\b",
            r"\bpremiere\b",
            r"\baired\b",
            r"\bairs\b",
            r"\bsimulcast\b",
        ],
    ),

    (
        "New Season",
        [
            r"\bseason\b",
            r"\bcour\b",
        ],
    ),

    (
        "New Trailer",
        [
            r"\btrailer\b",
            r"\bteaser\b",
            r"\bpreview\b",
        ],
    ),

    (
        "Release Date",
        [
            r"\brelease date\b",
            r"\bpremiere date\b",
            r"\bset to release\b",
            r"\bairs on\b",
            r"\bairing on\b",
        ],
    ),

    (
        "New Volume",
        [
            r"\bvolume\b",
            r"\bvol\.\b",
        ],
    ),

    (
        "Cast / Staff",
        [
            r"\bcast\b",
            r"\bstaff\b",
            r"\bvoices?\b",
            r"\bvoice actor\b",
            r"\bvoice actress\b",
            r"\badds .* cast\b",
        ],
    ),

    (
        "New Visual",
        [
            r"\bvisual\b",
            r"\bkey visual\b",
            r"\billustration\b",
        ],
    ),

    (
        "New Opening / Ending",
        [
            r"\bopening\b",
            r"\bending\b",
            r"\bcreditless\b",
            r"\btheme song\b",
        ],
    ),

    (
        "Adaptation",
        [
            r"\badaptation\b",
            r"\bgets an anime\b",
            r"\banime adaptation\b",
        ],
    ),

    (
        "Announcement",
        [
            r"\bannounc",
            r"\bunveils?\b",
            r"\breveals?\b",
            r"\bconfirmed\b",
            r"\bconfirms\b",
        ],
    ),

    (
        "Delay / Hiatus",
        [
            r"\bdelay",
            r"\bdelayed\b",
            r"\bpostpon",
            r"\bhiatus\b",
        ],
    ),

    (
        "Ending",
        [
            r"\bends?\b",
            r"\bending\b",
            r"\bconcludes?\b",
            r"\bfinal chapter\b",
            r"\bfinal volume\b",
        ],
    ),
]


def classify_topic(
    title,
    description
):

    text = (
        f"{title} {description}"
        .lower()
    )

    for label, patterns in TOPIC_RULES:

        for pattern in patterns:

            if re.search(
                pattern,
                text
            ):

                return label

    return "News"


# ============================================================
# SERIES / WORK NAME
# ============================================================

ACTION_WORDS = [

    "announces",
    "announced",

    "reveals",
    "revealed",

    "releases",
    "released",

    "shares",
    "shared",

    "drops",
    "dropped",

    "confirms",
    "confirmed",

    "gets",
    "receives",

    "ends",
    "ending",

    "concludes",
    "conclude",

    "delays",
    "delayed",

    "postpones",
    "postponed",

    "casts",
    "adds",

    "introduces",

    "unveils",
    "unveiled",

    "premieres",
    "premiered",

    "launches",

    "returns",

    "revealing",
]


def clean_series_name(name):

    name = re.sub(
        r"^\s*(new|latest|breaking)\s+",
        "",
        name,
        flags=re.I
    )

    name = re.sub(
        r"\s+\b(anime|manga|light novel|novel)\s*$",
        "",
        name,
        flags=re.I
    )

    name = re.sub(
        r"\s+",
        " ",
        name
    )

    name = name.strip(
        " -:|,"
    )

    return name


def extract_series_name(
    title,
    description=""
):

    title = clean_html_text(
        title
    )

    # --------------------------------------------------------
    # Quoted title
    # --------------------------------------------------------

    quoted = re.findall(
        r"['“\"]([^'”\"]{3,100})['”\"]",
        title
    )

    if quoted:

        return clean_series_name(
            max(
                quoted,
                key=len
            )
        )

    # --------------------------------------------------------
    # Split common separators
    # --------------------------------------------------------

    left = re.split(
        r"\s+[—–:|]\s+",
        title,
        maxsplit=1
    )[0].strip()

    lowered = left.lower()

    positions = []

    for action in ACTION_WORDS:

        match = re.search(
            rf"\b{re.escape(action)}\b",
            lowered
        )

        if match:
            positions.append(
                match.start()
            )

    if positions:

        candidate = left[
            :min(positions)
        ].strip()

        candidate = clean_series_name(
            candidate
        )

        if 2 <= len(candidate) <= 90:

            return candidate

    # --------------------------------------------------------
    # Remove generic framing
    # --------------------------------------------------------

    candidate = re.sub(
        r"^(here(?:'s| is) "
        r"(?:the )?"
        r"(?:exact )?"
        r"release date and time\s+)",
        "",
        title,
        flags=re.I
    )

    candidate = re.sub(
        r"^(new|latest)\s+",
        "",
        candidate,
        flags=re.I
    )

    # --------------------------------------------------------
    # Media marker
    # --------------------------------------------------------

    match = re.search(
        r"\s+(anime|manga|light novel|novel)\b",
        candidate,
        flags=re.I
    )

    if match:

        possible = candidate[
            :match.start()
        ].strip()

        possible = clean_series_name(
            possible
        )

        if 2 <= len(possible) <= 90:

            return possible

    # --------------------------------------------------------
    # Final fallback
    # --------------------------------------------------------

    candidate = clean_series_name(
        title
    )

    return shorten(
        candidate,
        80
    )


# ============================================================
# SHORT CONTEXT
# ============================================================

def make_context(
    title,
    description,
    topic,
    media_type
):

    desc = clean_html_text(
        description
    )

    title = clean_html_text(
        title
    )

    # Prefer RSS description
    if desc:

        summary = shorten(
            desc,
            260
        )

    else:

        summary = title

    # Don't repeat title as description
    if (
        summary.lower().strip(". ")
        == title.lower().strip(". ")
    ):

        summary = ""

    # Fallback descriptions
    if not summary:

        if topic == "New Episode":

            summary = (
                "A new episode update "
                "has been announced."
            )

        elif topic == "New Chapter":

            summary = (
                "A new chapter update "
                "has been announced."
            )

        elif topic == "New Trailer":

            summary = (
                "A new trailer or preview "
                "has been released."
            )

        elif topic == "Release Date":

            summary = (
                "A release-date update "
                "has been announced."
            )

        else:

            summary = (
                "A new update has "
                "been reported."
            )

    return summary


# ============================================================
# DATES
# ============================================================

def parse_entry_datetime(entry):

    parsed = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )

    if parsed:

        try:

            return datetime(
                parsed.tm_year,
                parsed.tm_mon,
                parsed.tm_mday,
                parsed.tm_hour,
                parsed.tm_min,
                parsed.tm_sec,
                tzinfo=timezone.utc,
            )

        except (
            AttributeError,
            ValueError
        ):
            pass

    return datetime.now(
        timezone.utc
    )


def format_datetime(dt):

    # Discord understands ISO timestamps
    # and renders them according to the
    # viewer's locale.

    return dt.isoformat()

# ============================================================
# SMART DUPLICATE DETECTION
# ============================================================

STOP_WORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "from",
    "by",
    "is",
    "are",
    "be",
    "its",
    "this",
    "that",
    "new",
    "latest",
    "official",
    "reveals",
    "revealed",
    "announces",
    "announced",
    "confirms",
    "confirmed",
    "unveils",
    "unveiled",
    "shares",
    "shared",
    "gets",
    "getting",
    "release",
    "released",
    "news",
}

ROMANIZATION_REPLACEMENTS = {
    "ā": "a",
    "á": "a",
    "à": "a",
    "ä": "a",

    "ē": "e",
    "é": "e",
    "è": "e",
    "ë": "e",

    "ī": "i",
    "í": "i",
    "ì": "i",
    "ï": "i",

    "ō": "o",
    "ó": "o",
    "ò": "o",
    "ö": "o",

    "ū": "u",
    "ú": "u",
    "ù": "u",
    "ü": "u",
}


def normalize_romanization(text):
    """
    Normalize common romanized Japanese characters.

    Example:
        Hyōka -> Hyouka
    """

    text = text.lower()

    for old, new in ROMANIZATION_REPLACEMENTS.items():
        text = text.replace(old, new)

    return text
def normalize_title(title):

    text = clean_html_text(
        title
    ).lower()
    
    text = normalize_romanization(
        text
    )

    # Decode punctuation/entities
    text = html.unescape(text)

    # Normalize common punctuation
    text = text.replace(
        "’",
        "'"
    )
    
    # Normalize common spacing variations
    text = re.sub(
        r"\bno\s+da\s+ga\b",
        "nodaga",
        text
    )

    text = text.replace(
        "–",
        " "
    )

    text = text.replace(
        "—",
        " "
    )

    # Remove URLs
    text = re.sub(
        r"https?://\S+",
        " ",
        text
    )

    # Keep letters/numbers
    text = re.sub(
        r"[^a-z0-9\s]",
        " ",
        text
    )

    # Tokenize
    tokens = text.split()

    # Remove generic words
    tokens = [
        token
        for token in tokens
        if token not in STOP_WORDS
    ]

    return " ".join(tokens)


def title_tokens(title):

    normalized = normalize_title(
        title
    )

    return set(
        normalized.split()
    )


def token_similarity(
    title_a,
    title_b
):

    tokens_a = title_tokens(
        title_a
    )

    tokens_b = title_tokens(
        title_b
    )

    if not tokens_a or not tokens_b:
        return 0.0

    intersection = (
        tokens_a & tokens_b
    )

    union = (
        tokens_a | tokens_b
    )

    return (
        len(intersection)
        / len(union)
    )


def sequence_similarity(
    title_a,
    title_b
):

    normalized_a = normalize_title(
        title_a
    )

    normalized_b = normalize_title(
        title_b
    )

    if not normalized_a or not normalized_b:
        return 0.0

    return difflib.SequenceMatcher(
        None,
        normalized_a,
        normalized_b
    ).ratio()


def extract_keywords(
    title,
    description=""
):

    text = (
        f"{title} {description}"
    )

    normalized = normalize_title(
        text
    )

    tokens = normalized.split()

    # Remove very short words
    tokens = [
        token
        for token in tokens
        if len(token) >= 3
    ]

    return set(tokens)


def keyword_similarity(
    title_a,
    description_a,
    title_b,
    description_b
):

    keywords_a = extract_keywords(
        title_a,
        description_a
    )

    keywords_b = extract_keywords(
        title_b,
        description_b
    )

    if not keywords_a or not keywords_b:
        return 0.0

    common = (
        keywords_a & keywords_b
    )

    return (
        len(common)
        / max(
            1,
            min(
                len(keywords_a),
                len(keywords_b)
            )
        )
    )


def extract_story_identity(
    title,
    description=""
):

    """
    Build a rough identity for the story.

    Example:

    The Apothecary Diaries Season 3
    Reveals New Trailer

    becomes roughly:

    apothecary diaries season 3
    """

    normalized = normalize_title(
        title
    )

    tokens = normalized.split()

    # Important franchise/content tokens
    important = []

    media_words = {
        "season",
        "chapter",
        "episode",
        "volume",
        "movie",
        "film",
        "trailer",
        "visual",
        "opening",
        "ending",
        "adaptation",
        "anime",
        "manga",
        "novel",
    }

    for token in tokens:

        if (
            token in media_words
            or len(token) >= 4
        ):

            important.append(
                token
            )

    return " ".join(
        important
    )


def story_similarity(
    entry_a,
    entry_b
):

    title_a = clean_html_text(
        entry_a.get(
            "title",
            ""
        )
    )

    title_b = clean_html_text(
        entry_b.get(
            "title",
            ""
        )
    )

    description_a = clean_html_text(
        entry_a.get(
            "summary",
            ""
        )
        or entry_a.get(
            "description",
            ""
        )
    )

    description_b = clean_html_text(
        entry_b.get(
            "summary",
            ""
        )
        or entry_b.get(
            "description",
            ""
        )
    )

    if not title_a or not title_b:
        return 0.0

    # --------------------------------------------------------
    # 1. Token similarity
    # --------------------------------------------------------

    token_score = token_similarity(
        title_a,
        title_b
    )

    # --------------------------------------------------------
    # 2. Sequence similarity
    # --------------------------------------------------------

    sequence_score = sequence_similarity(
        title_a,
        title_b
    )

    # --------------------------------------------------------
    # 3. Keyword similarity
    # --------------------------------------------------------

    keyword_score = keyword_similarity(
        title_a,
        description_a,
        title_b,
        description_b
    )

    # --------------------------------------------------------
    # 4. Story identity
    # --------------------------------------------------------

    identity_a = extract_story_identity(
        title_a,
        description_a
    )

    identity_b = extract_story_identity(
        title_b,
        description_b
    )

    identity_score = sequence_similarity(
        identity_a,
        identity_b
    )

    # --------------------------------------------------------
    # Weighted score
    # --------------------------------------------------------

    score = (
        token_score * 0.35
        + sequence_score * 0.25
        + keyword_score * 0.20
        + identity_score * 0.20
    )

    return score


def same_story(
    entry_a,
    entry_b
):

    title_a = clean_html_text(
        entry_a.get(
            "title",
            ""
        )
    )

    title_b = clean_html_text(
        entry_b.get(
            "title",
            ""
        )
    )

    if not title_a or not title_b:
        return False

    # --------------------------------------------------------
    # Calculate base similarity
    # --------------------------------------------------------

    score = story_similarity(
        entry_a,
        entry_b
    )

    # --------------------------------------------------------
    # Strong direct match
    # --------------------------------------------------------

    if score >= 0.72:
        return True

    # --------------------------------------------------------
    # Very similar titles
    # --------------------------------------------------------

    title_score = sequence_similarity(
        title_a,
        title_b
    )

    if title_score >= 0.90:
        return True

    # --------------------------------------------------------
    # Token overlap
    # --------------------------------------------------------

    tokens_a = title_tokens(
        title_a
    )

    tokens_b = title_tokens(
        title_b
    )

    if tokens_a and tokens_b:

        common = (
            tokens_a & tokens_b
        )

        smaller = min(
            len(tokens_a),
            len(tokens_b)
        )

        overlap = (
            len(common) / smaller
        )

        # If most of the important words
        # are shared, they're probably the
        # same story.
        if (
            smaller >= 3
            and overlap >= 0.75
        ):
            return True

    # --------------------------------------------------------
    # Series identity matching
    # --------------------------------------------------------

    identity_a = extract_story_identity(
        title_a
    )

    identity_b = extract_story_identity(
        title_b
    )

    if identity_a and identity_b:

        identity_score = sequence_similarity(
            identity_a,
            identity_b
        )

        # Strong series identity + reasonable
        # overall similarity.
        if (
            identity_score >= 0.85
            and score >= 0.55
        ):
            return True

    # --------------------------------------------------------
    # Moderate similarity + strong keyword match
    # --------------------------------------------------------

    description_a = clean_html_text(
        entry_a.get(
            "summary",
            ""
        )
        or entry_a.get(
            "description",
            ""
        )
    )

    description_b = clean_html_text(
        entry_b.get(
            "summary",
            ""
        )
        or entry_b.get(
            "description",
            ""
        )
    )

    keyword_score = keyword_similarity(
        title_a,
        description_a,
        title_b,
        description_b
    )

    if (
        score >= 0.58
        and keyword_score >= 0.75
    ):
        return True

    return False

def already_posted(
    entry,
    source,
    state
):

    link = entry.get(
        "link",
        ""
    ).strip()

    guid = entry.get(
        "id",
        ""
    ).strip()

    posted = state.get(
        "posted",
        {}
    )

    # --------------------------------------------------------
    # Exact URL
    # --------------------------------------------------------

    if link and link in posted:
        return True

    # --------------------------------------------------------
    # Exact RSS GUID
    # --------------------------------------------------------

    if guid and guid in posted:
        return True

    # --------------------------------------------------------
    # Smart cross-feed duplicate detection
    # --------------------------------------------------------

    for item in posted.values():

        old_entry = {
            "title": item.get(
                "title",
                ""
            ),
            "summary": item.get(
                "description",
                ""
            ),
        }

        if same_story(
            entry,
            old_entry
        ):

            return True

    return False


def find_matching_story(
    entry,
    state
):

    """
    Returns the previously stored article
    if this article appears to be the same story.

    Otherwise returns None.
    """

    for key, item in state.get(
        "posted",
        {}
    ).items():

        old_entry = {
            "title": item.get(
                "title",
                ""
            ),
            "summary": item.get(
                "description",
                ""
            ),
        }

        if same_story(
            entry,
            old_entry
        ):

            return {
                "key": key,
                "data": item,
            }

    return None


def remember_entry(
    entry,
    source,
    state
):

    
    key = (
        entry.get("link")
        or entry.get("id")
        or normalize_title(
            entry.get(
                "title",
                ""
            )
        )
    )

    state["posted"][key] = {

        "title": clean_html_text(
            entry.get(
                "title",
                ""
            )
        ),

        "description": clean_html_text(
            entry.get(
                "summary",
                ""
            )
            or entry.get(
                "description",
                ""
            )
        ),

        "source": source,

        "sources": [
            source
        ],

        "link": entry.get(
            "link",
            ""
        ),

        "time": datetime.now(
            timezone.utc
        ).isoformat(),
    }


def add_source_to_story(
    entry,
    source,
    state
):

    match = find_matching_story(
        entry,
        state
    )

    if not match:
        return False

    item = match["data"]

    sources = item.setdefault(
        "sources",
        []
    )

    if source not in sources:

        sources.append(
            source
        )

    return True

# ============================================================
# DISCORD
# ============================================================

def send_embed(
    entry,
    source,
    dry_run=False
):

    title = clean_html_text(
        entry.get(
            "title",
            "Untitled"
        )
    )

    link = entry.get(
        "link",
        ""
    ).strip()

    description = (
        entry.get("summary")
        or entry.get("description")
        or ""
    )

    # Determine media type
    media_type = classify_type(
        title,
        description
    )

    # Determine news topic
    topic = classify_topic(
        title,
        description
    )

    # Determine anime/manga/novel name
    series = extract_series_name(
        title,
        description
    )

    # Generate compact context
    context = make_context(
        title,
        description,
        topic,
        media_type
    )

    # Get image
    image_url = get_entry_image(
        entry
    )

    # Website image fallback
    if not image_url:

        image_url = get_og_image(
            link
        )

    published = parse_entry_datetime(
        entry
    )

    # ========================================================
    # DISCORD EMBED
    # ========================================================

    embed = {

        # Anime / manga / novel name
        "title": series[:256],

        # Clicking title opens article
        "url": link,

        # Compact content
        "description": (
            f"**{media_type}**  •  **{topic}**\n\n"
            f"{context}"
        )[:4096],

        # Random color
        "color": random_embed_color(),

        # Published timestamp
        "timestamp": format_datetime(
            published
        ),

        # Footer
        "footer": {
            "text": (
                f"{source}  •  Anime News"
            )
        },

        # Small metadata fields
        "fields": [

            {
                "name": "Source",
                "value": source,
                "inline": True,
            },

            {
                "name": "Type",
                "value": media_type,
                "inline": True,
            },
        ],
    }

    # Add article image
    if image_url:

        embed["image"] = {
            "url": image_url
        }

    payload = {
        "embeds": [
            embed
        ]
    }

    # Optional role mention
    if MENTION_ROLE_ID:

        payload["content"] = (
            f"<@&{MENTION_ROLE_ID}>"
        )

        payload[
            "allowed_mentions"
        ] = {
            "roles": [
                MENTION_ROLE_ID
            ]
        }

    # --------------------------------------------------------
    # TEST / DEBUG
    # --------------------------------------------------------

    if dry_run:

        print(
            "\n--- TEST EMBED ---"
        )

        print(
            json.dumps(
                payload,
                indent=2,
                ensure_ascii=False
            )
        )

        return True

    # --------------------------------------------------------
    # WEBHOOK CHECK
    # --------------------------------------------------------

    if not WEBHOOK_URL:

        print(
            "ERROR: "
            "DISCORD_WEBHOOK_URL "
            "is missing."
        )

        return False

    # --------------------------------------------------------
    # SEND TO DISCORD
    # --------------------------------------------------------

    try:

        response = session.post(
            WEBHOOK_URL,
            json=payload,
            timeout=20
        )

        if response.status_code in (
            200,
            204
        ):

            print(
                f"Posted: {series} "
                f"[{media_type} / {topic}]"
            )

            return True

        print(
            f"Discord error "
            f"{response.status_code}: "
            f"{response.text[:500]}"
        )

        return False

    except requests.RequestException as error:

        print(
            f"Discord request failed: "
            f"{error}"
        )

        return False


# ============================================================
# RSS FEED HANDLING
# ============================================================

def fetch_feed(feed):

    name = feed["name"]
    url = feed["url"]

    try:

        response = session.get(
            url,
            timeout=20
        )

        response.raise_for_status()

        parsed = feedparser.parse(
            response.content
        )

        if (
            parsed.bozo
            and not parsed.entries
        ):

            print(
                f"[WARN] {name}: "
                "invalid/blocked RSS feed."
            )

            return None

        return parsed

    except (
        requests.RequestException,
        ValueError
    ) as error:

        print(
            f"[ERROR] {name}: "
            f"{error}"
        )

        return None


# ============================================================
# FIRST RUN SEED
# ============================================================

def seed_existing_entries(
    feeds,
    state
):

    """
    Mark existing RSS entries as known.

    This prevents the first run from posting
    12 old articles from every feed.
    """

    print(
        "First run: "
        "seeding existing RSS entries..."
    )

    for feed in feeds:

        parsed = fetch_feed(
            feed
        )

        if not parsed:
            continue

        entries = parsed.entries[
            :RSS_ITEMS_TO_SCAN
        ]

        for entry in entries:

            remember_entry(
                entry,
                feed["name"],
                state
            )

    state[
        "initialized"
    ] = True

    save_state(
        state
    )

    print(
        "Seed complete. "
        "Waiting for NEW articles."
    )


# ============================================================
# COLLECT NEW ARTICLES
# ============================================================

def collect_new_entries(
    feeds,
    state
):

    candidates = []

    for feed in feeds:

        name = feed["name"]

        print(
            f"Checking: {name}"
        )

        parsed = fetch_feed(
            feed
        )

        if not parsed:
            continue

        entries = parsed.entries[
            :RSS_ITEMS_TO_SCAN
        ]

        for entry in entries:

            if already_posted(
                entry,
                name,
                state
            ):
                if add_source_to_story(
                    entry,
                    name,
                    state
                ):
                    save_state(state)
                continue

            candidates.append({

                "entry": entry,

                "source": name,

                "published":
                    parse_entry_datetime(
                        entry
                    ),
            })

    # Oldest first
    candidates.sort(
        key=lambda item:
        item["published"]
    )

    return candidates


# ============================================================
# RUN ONE RSS CYCLE
# ============================================================

def run_cycle():

    feeds = load_feeds()

    state = load_state()

    # First run
    if (
        not state["initialized"]
        and SEED_EXISTING_ON_FIRST_RUN
    ):

        seed_existing_entries(
            feeds,
            state
        )

        return

    # Find new articles
    candidates = collect_new_entries(
        feeds,
        state
    )

    if not candidates:

        print(
            "No new articles."
        )

        return

    posted_this_cycle = 0

    for item in candidates:

        # Respect posting limit
        if (
            posted_this_cycle
            >= MAX_POSTS_PER_CYCLE
        ):

            print(
                "Reached "
                f"MAX_POSTS_PER_CYCLE="
                f"{MAX_POSTS_PER_CYCLE}."
            )

            print(
                "Remaining articles "
                "stay for next cycle."
            )

            break

        entry = item["entry"]

        source = item["source"]

        # Send
        if send_embed(
            entry,
            source
        ):

            remember_entry(
                entry,
                source,
                state
            )

            posted_this_cycle += 1

            save_state(
                state
            )

    # IMPORTANT:
    # Articles not posted because of the limit
    # are NOT marked as posted.


# ============================================================
# TEST MODE
# ============================================================

def test_latest():

    feeds = load_feeds()

    print(
        "TEST MODE: "
        "posting one latest article "
        "per feed.\n"
    )

    for feed in feeds:

        parsed = fetch_feed(
            feed
        )

        if (
            not parsed
            or not parsed.entries
        ):

            print(
                f"No entries: "
                f"{feed['name']}"
            )

            continue

        entry = parsed.entries[0]

        send_embed(
            entry,
            feed["name"],
            dry_run=False
        )

def test_duplicate_engine():

    feeds = load_feeds()

    state = load_state()

    print(
        "\n"
        "========================================\n"
        " SMART DUPLICATE DETECTION TEST\n"
        "========================================\n"
    )

    all_entries = []

    for feed in feeds:

        parsed = fetch_feed(
            feed
        )

        if not parsed:
            continue

        for entry in parsed.entries[:8]:

            all_entries.append(
                (
                    feed["name"],
                    entry
                )
            )

    print(
        f"Collected "
        f"{len(all_entries)} articles.\n"
    )

    # Compare every article against
    # every other article.
    for i in range(
        len(all_entries)
    ):

        source_a, entry_a = (
            all_entries[i]
        )

        for j in range(
            i + 1,
            len(all_entries)
        ):

            source_b, entry_b = (
                all_entries[j]
            )

            score = story_similarity(
                entry_a,
                entry_b
            )

            if score >= 0.50:

                title_a = clean_html_text(
                    entry_a.get(
                        "title",
                        ""
                    )
                )

                title_b = clean_html_text(
                    entry_b.get(
                        "title",
                        ""
                    )
                )

                print(
                    "\n----------------------------------------"
                )

                print(
                    f"Score: {score:.2f}"
                )

                print(
                    f"{source_a}:"
                )

                print(
                    f"  {title_a}"
                )

                print(
                    f"{source_b}:"
                )

                print(
                    f"  {title_b}"
                )

                print(
                    "SAME STORY:",
                    same_story(
                        entry_a,
                        entry_b
                    )
                )

# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Anime RSS → "
            "Discord webhook bot"
        )
    )

    parser.add_argument(
        "--test",
        action="store_true",
        help=(
            "Post the latest article "
            "from each feed immediately."
        ),
    )
    parser.add_argument(
    "--test-duplicates",
    action="store_true",
    help=(
        "Test smart duplicate detection "
        "without posting to Discord."
    ),
)

    args = parser.parse_args()
    
    if args.test_duplicates:

        test_duplicate_engine()

        return

    # Test mode
    if args.test:

        test_latest()

        return

    # Startup information
    print(
        "=" * 50
    )

    print(
        " Anime News → Discord"
    )

    print(
        "=" * 50
    )

    print(
        f"Feeds: "
        f"{len(load_feeds())}"
    )

    print(
        "Check interval: "
        f"{CHECK_INTERVAL_SECONDS // 60} minutes"
    )

    print(
        "Max posts/cycle: "
        f"{MAX_POSTS_PER_CYCLE}"
    )

    print(
        "Color palette: "
        f"{len(EMBED_COLORS)} colors"
    )

    print(
        "=" * 50
    )

    # Infinite loop
    while True:

        try:

            print(
                "\nChecking RSS feeds...\n"
            )

            run_cycle()

        except KeyboardInterrupt:

            print(
                "\nStopped."
            )

            break

        except Exception as error:

            print(
                "[UNEXPECTED ERROR] "
                f"{error}"
            )

        print(
            "\nNext check in "
            f"{CHECK_INTERVAL_SECONDS // 60} minutes..."
        )

        try:

            time.sleep(
                CHECK_INTERVAL_SECONDS
            )

        except KeyboardInterrupt:

            print(
                "\nStopped."
            )

            break


if __name__ == "__main__":
    main()