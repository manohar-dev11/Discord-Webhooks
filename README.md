# Anime News → Discord

A small Python bot that reads anime / manga / light-novel news from RSS feeds and posts
compact, de-duplicated Discord embeds through a webhook. No database, no framework.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then paste your webhook URL into .env
python bot.py --test             # checks config + feeds, prints a sample embed (sends nothing)
python bot.py --dry-run          # full cycle, prints what WOULD be posted
python bot.py                    # run continuously
```

## Commands

| Command | What it does |
|---|---|
| `python bot.py` | Run forever (checks every `CHECK_INTERVAL_MINUTES`, default 15) |
| `python bot.py --once` | One cycle, then exit (for cron / schedulers) |
| `python bot.py --dry-run` | One cycle, prints embeds, posts and saves nothing |
| `python bot.py --test` | Config, feed and sample-embed check. Never posts. |
| `python bot.py --test --send` | Same, and sends one sample embed |
| `python bot.py --test-media` | Anime/Manga/Novel classification test (known cases + real feeds) |
| `python bot.py --test-duplicates` | Cross-source duplicate test (known cases + real feeds) |

## First run

On the first normal run the bot **seeds** `posted.json` with everything currently in the
feeds and posts nothing. Only articles published afterwards are posted. Set
`FIRST_RUN_POST_LATEST=2` if you want the newest 2 posted immediately.

## How it works

1. **Fetch** every feed independently. A dead feed, timeout, invalid XML or a Cloudflare
   "Just a moment…" page is logged and skipped; the others continue. Cloudflare is never bypassed.
2. **Clean** HTML, entities, tracking parameters (`utm_*`, `fbclid`, …).
3. **Classify** with scores: Anime / Manga / Novel / Other. Source-medium words ("Light Novel X
   Gets TV Anime") don't override the real news event. "Graphic/visual novel" → Other.
4. **Extract** the work's name, the primary event (priority-ordered), a 1–3 sentence factual
   context built from the headline/description, and an image.
5. **Filter** against `posted.json`, then **cluster** same-story articles across sources using
   entity similarity + title similarity + event overlap (+ chapter/episode/season numbers, which
   keep "Chapter 1170" and "Chapter 1171" apart). The best source/image wins.
6. **Rank**, then post at most `MAX_POSTS_PER_CYCLE` (3), and at most one story per franchise
   per cycle. Unposted articles are *not* remembered, so they stay eligible next cycle.
7. **Remember** the posted story and every other source's version of it.

Embed images are verified before use (and Open Graph is tried if the feed has none).
If an image fails, the embed is sent without it.

## Configuration

- **Feeds:** edit `RSS_FEEDS` at the top of `bot.py`. Order = priority when sources overlap.
  Feed URLs change over time. Run `python bot.py --test` to confirm each one works from your
  server, and replace any that report a problem.
- **Thresholds:** `SAME_STORY_SCORE`, `CONTEXT_SCORE`, `SAME_SOURCE_SCORE` in `bot.py`.
- **Events / media signals:** `EVENT_RULES`, `REVEAL_ITEMS`, `*_SIGNALS` tables in `bot.py`.
  Add a known miss to `MEDIA_TEST_CASES` / `DUPLICATE_TEST_CASES` and re-run the tests.

## Running 24/7

systemd (Linux):

```ini
# /etc/systemd/system/anime-news.service
[Unit]
Description=Anime News Discord bot
After=network-online.target

[Service]
WorkingDirectory=/path/to/Discord-Webhook
ExecStart=/path/to/Discord-Webhook/.venv/bin/python bot.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Or use cron with `--once`: `*/15 * * * * cd /path/to/Discord-Webhook && .venv/bin/python bot.py --once`.

## Security

- The webhook URL lives only in `.env` (git-ignored) and is redacted from logs.
- If a webhook is ever exposed, delete it in Discord and create a new one immediately.
- If you committed `.env` or `posted.json` by mistake: `git rm --cached .env posted.json`,
  commit, and rotate the webhook.
- Messages never use `@everyone`/`@here`. A role is pinged only if `DISCORD_ROLE_ID` is set,
  and only on the first post of a cycle.

## Files

```
bot.py            the whole application
requirements.txt  feedparser, requests, python-dotenv, beautifulsoup4
.env.example      template for .env (copy it; never commit .env)
.gitignore        .env, posted.json, __pycache__/, *.pyc
posted.json       created automatically (state)
```