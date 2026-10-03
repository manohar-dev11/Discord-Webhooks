# Anime News → Discord

A Python RSS bot that sends compact anime, manga and novel news
to a Discord channel using a Discord webhook.

## Features

- 4 RSS feeds
- One Discord webhook
- 1400+ generated random colors
- No identical color twice in a row
- Compact mobile-friendly Discord embeds
- Anime / Manga / Novel classification
- New Episode detection
- New Chapter detection
- New Season detection
- Trailer detection
- Release Date detection
- New Volume detection
- Cast / Staff detection
- New Visual detection
- Opening / Ending detection
- Adaptation detection
- Announcement detection
- Delay / Hiatus detection
- Ending detection
- Anime / manga / novel name extraction
- Short RSS context
- HTML cleanup
- HTML entity cleanup
- RSS image extraction
- Website `og:image` fallback
- Cross-feed duplicate detection
- Persistent article history
- First-run protection
- Maximum posts per cycle
- Optional Discord role mention
- Test mode

---

# Installation

## 1. Create virtual environment

Windows:

    python -m venv .venv

Activate:

    .venv\Scripts\activate

Linux/macOS:

    python3 -m venv .venv

    source .venv/bin/activate

---

# 2. Install packages

    pip install -r requirements.txt

---

# 3. Configure Discord

Create a webhook in your Discord server:

Server Settings
→ Integrations
→ Webhooks
→ New Webhook

Select your `#anime-news` channel.

Copy the webhook URL.

Create a `.env` file:

    DISCORD_WEBHOOK_URL=YOUR_WEBHOOK_URL

Do NOT publish your webhook URL.

---

# 4. Test the bot

Run:

    python bot.py --test

This sends the latest article from each of the
four RSS feeds.

You should receive up to four test messages.

---

# 5. Start normal mode

Run:

    python bot.py

The first normal run will seed existing RSS articles.

It will NOT dump the existing RSS backlog into Discord.

After that, the bot checks for new articles every 15 minutes.

---

# Posting limit

The default maximum is:

    MAX_POSTS_PER_CYCLE = 3

This means a maximum of three new articles can be posted
during a single RSS check.

If you want two:

    MAX_POSTS_PER_CYCLE = 2

If you want five:

    MAX_POSTS_PER_CYCLE = 5

---

# RSS check interval

Default:

    CHECK_INTERVAL_SECONDS = 15 * 60

This means every 15 minutes.

For 30 minutes:

    CHECK_INTERVAL_SECONDS = 30 * 60

For 1 hour:

    CHECK_INTERVAL_SECONDS = 60 * 60

---

# Feeds

The bot currently uses:

1. Anime News Network
2. Crunchyroll News
3. MyAnimeList
4. Anime Corner

Edit `feeds.json` if you want to add or remove feeds.

---

# Discord message design

Each message attempts to contain:

- Anime / Manga / Novel name
- Media type
- News topic
- Short context
- Article image
- Source
- Publication timestamp
- Clickable article title
- Random embed color

Example:

    The Apothecary Diaries

    Anime • New Episode

    The series revealed its creditless opening
    ahead of today's premiere.

    Source: Anime Corner
    Type: Anime

    [article image]

---

# Duplicate protection

The bot checks:

- Article URL
- RSS GUID
- Similar article titles

This helps prevent the same story from multiple RSS feeds
from being posted repeatedly.

---

# State

`posted.json` stores previously processed articles.

Do not delete it unless you intentionally want to reset
the bot's article history.

If you delete `posted.json`, the bot will treat the next
run as a fresh installation.

---

# Security

Never publish:

    .env

Never publish:

    DISCORD_WEBHOOK_URL

Never send your Discord webhook URL to other people.

The `.gitignore` file already excludes `.env`.

---

# Stop the bot

Press:

    CTRL + C

---

# Run again

    python bot.py