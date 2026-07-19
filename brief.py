#!/usr/bin/env python3
"""
Daily brief: weather, Denmark news, world news, game releases, tech.

Publishes two ways, both free and both inside GitHub:
  * writes docs/index.html  -> served by GitHub Pages as a styled page
  * opens a GitHub Issue    -> pushes a notification to the GitHub mobile app

Environment variables (Actions provides the first two automatically):
  GITHUB_TOKEN      required — posts the issue
  GITHUB_REPOSITORY required — e.g. "ullus/daily-brief"
  ANTHROPIC_API_KEY optional — rewrites headlines into one-line summaries.
                    Without it you get raw headlines.
  FORCE_RUN         optional — "1" bypasses the 07:00 local-time check
  DRY_RUN           optional — "1" writes files but posts no issue
"""

import os
import re
import sys
import html
import json
import datetime as dt
from zoneinfo import ZoneInfo

import requests
import feedparser

TZ = ZoneInfo("Europe/Copenhagen")
LAT, LON = 55.6761, 12.5683
CITY = "Copenhagen"
SEND_HOUR = 7

FEEDS = {
    "Denmark": [
        "https://www.dr.dk/nyheder/service/feeds/allenyheder",
        "https://www.thelocal.dk/feeds/rss.php",
    ],
    "World": [
        # Three vantage points on purpose: British public broadcaster,
        # Qatari-funded international, German public broadcaster. Where they
        # agree you can be fairly confident; where they diverge is informative.
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://rss.dw.com/rdf/rss-en-world",
    ],
    "Games": [
        "https://www.pcgamer.com/rss/",
        "https://www.eurogamer.net/feed",
    ],
    "Tech": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
    ],
}

ITEMS_PER_SECTION = 5
TIMEOUT = 20
API = "https://api.github.com"

# --- AI summarisation cost controls -----------------------------------------
# Every call bills your Anthropic account, so these three settings are the
# difference between pennies and pounds a month.
#
#   MODEL          claude-haiku-4-5-20251001 is roughly a tenth the price of
#                  Sonnet and fine for rewriting headlines. Swap to
#                  "claude-sonnet-5" if you want better prose.
#   OVERFETCH      how many candidate stories per section the model chooses
#                  from. 2x ITEMS_PER_SECTION is plenty; higher costs more.
#   SUMMARY_CHARS  how much of each article the model sees. Enough for context,
#                  not the whole piece.
AI_MODEL = "claude-haiku-4-5-20251001"
AI_OVERFETCH = 2
AI_SUMMARY_CHARS = 200


# ---------------------------------------------------------------- weather

WEATHER_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 80: "Showers",
    81: "Showers", 82: "Heavy showers", 95: "Thunderstorms",
    96: "Thunderstorms with hail", 99: "Severe thunderstorms",
}

WEATHER_EMOJI = {
    0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️", 45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌦️", 55: "🌧️", 61: "🌦️", 63: "🌧️", 65: "🌧️",
    71: "🌨️", 73: "🌨️", 75: "❄️", 80: "🌦️", 81: "🌧️", 82: "⛈️",
    95: "⛈️", 96: "⛈️", 99: "🌩️",
}

# Background wash behind the weather hero, by rough condition family.
HERO_TINT = {
    "clear":  ("#f9d976", "#f39f52"),
    "cloud":  ("#8ea6c0", "#5d7793"),
    "rain":   ("#5b8fc9", "#33587f"),
    "snow":   ("#cfe3f2", "#93b6d4"),
    "storm":  ("#6c5ce7", "#3b3070"),
    "fog":    ("#b6bcc4", "#7e858e"),
}


def hero_tint(code):
    if code in (0, 1):
        return HERO_TINT["clear"]
    if code in (45, 48):
        return HERO_TINT["fog"]
    if code in (71, 73, 75):
        return HERO_TINT["snow"]
    if code in (95, 96, 99):
        return HERO_TINT["storm"]
    if code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
        return HERO_TINT["rain"]
    return HERO_TINT["cloud"]


# Emoji and accent colour per section.
SECTION_STYLE = {
    "Power":       ("🔌", "#f2a541"),
    "Denmark":     ("🇩🇰", "#c8102e"),
    "World":       ("🌍", "#1a6fb4"),
    "Games":       ("🎮", "#7c4dff"),
    "Tech":        ("⚡", "#0f9b8e"),
    "GitHub":      ("🐙", "#6e5494"),
    "Hacker News": ("🔶", "#ff6600"),
    "Football":    ("⚽", "#2d8a4e"),
    "Superliga":   ("🏟️", "#c8102e"),
    "Music":       ("🎵", "#e0245e"),
    "Jobs":        ("💼", "#b8860b"),
}


def fetch_weather():
    """Today's forecast from Open-Meteo. No API key needed."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
        "wind_speed_10m_max,weather_code,sunrise,sunset"
        "&timezone=Europe/Copenhagen&forecast_days=1"
    )
    try:
        d = requests.get(url, timeout=TIMEOUT).json()["daily"]
        code = d["weather_code"][0]
        return {
            "high": round(d["temperature_2m_max"][0]),
            "low": round(d["temperature_2m_min"][0]),
            "rain": d["precipitation_probability_max"][0],
            "wind": round(d["wind_speed_10m_max"][0]),
            "code": code,
            "cond": WEATHER_CODES.get(code, "—"),
            "emoji": WEATHER_EMOJI.get(code, "🌡️"),
            "sunrise": d["sunrise"][0][11:16],
            "sunset": d["sunset"][0][11:16],
        }
    except Exception as e:
        print(f"  ! weather failed: {e}", file=sys.stderr)
        return None


# ------------------------------------------------------------------ feeds


def fetch_section(urls, limit):
    """Pull recent entries from a set of RSS feeds, newest first."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=36)
    items = []
    for url in urls:
        try:
            parsed = feedparser.parse(url)
            if parsed.bozo and not parsed.entries:
                print(f"  ! feed unreadable: {url}", file=sys.stderr)
                continue
            for e in parsed.entries[:15]:
                when = e.get("published_parsed") or e.get("updated_parsed")
                ts = (
                    dt.datetime(*when[:6], tzinfo=dt.timezone.utc)
                    if when else dt.datetime.now(dt.timezone.utc)
                )
                if ts < cutoff:
                    continue
                items.append({
                    "title": e.get("title", "").strip(),
                    "url": e.get("link", ""),
                    "summary": strip_tags(e.get("summary", ""))[:400],
                    "image": extract_image(e),
                    "ts": ts,
                })
        except Exception as e:
            print(f"  ! feed error {url}: {e}", file=sys.stderr)

    items.sort(key=lambda i: i["ts"], reverse=True)
    seen, unique = set(), []
    for i in items:
        key = "".join(c for c in i["title"].lower() if c.isalnum())[:40]
        if key in seen:
            continue
        seen.add(key)
        unique.append(i)
    return unique[:limit]


def extract_image(entry):
    """
    Find a thumbnail for an article. Feeds advertise images in several
    different ways, so try each in turn and give up quietly.
    """
    for key in ("media_content", "media_thumbnail"):
        media = entry.get(key) or []
        for m in media:
            url = m.get("url")
            if url and not url.endswith(".svg"):
                return url

    for enc in entry.get("enclosures", []) or []:
        if enc.get("type", "").startswith("image/") and enc.get("href"):
            return enc["href"]

    # Last resort: the first <img> inside the description HTML.
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', entry.get("summary", ""), re.I)
    if m and m.group(1).startswith("http"):
        return m.group(1)
    return None


def strip_tags(s):
    out, depth = [], 0
    for c in s:
        if c == "<":
            depth += 1
        elif c == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(c)
    return " ".join("".join(out).split())


# ------------------------------------------------------- extra data sources
#
# These need no API key. Each returns None or [] on failure so a dead source
# costs you one section rather than the whole brief.


def fetch_power():
    """
    Today's electricity spot prices for DK2 (Zealand, incl. Copenhagen) from
    Energi Data Service. Prices are DKK per MWh; divide by 10 for øre per kWh.

    Note this is the raw spot price — your actual bill adds transport tariffs,
    taxes and VAT, so treat these as relative rather than absolute.
    """
    today = dt.datetime.now(TZ).date()
    tomorrow = today + dt.timedelta(days=1)
    base = "https://api.energidataservice.dk/dataset/Elspotprices"

    # Two attempts: the precise query first, then a looser one. If naming their
    # columns or sort order is what's breaking it, the second still works.
    attempts = [
        {
            "start": f"{today}T00:00",
            "end": f"{tomorrow}T00:00",
            "filter": json.dumps({"PriceArea": ["DK2"]}),
            "columns": "HourDK,SpotPriceDKK",
            "sort": "HourDK ASC",
            "limit": 100,
        },
        {
            "start": f"{today}T00:00",
            "end": f"{tomorrow}T00:00",
            "filter": json.dumps({"PriceArea": ["DK2"]}),
            "limit": 100,
        },
    ]

    for n, params in enumerate(attempts, 1):
        try:
            r = requests.get(base, params=params, timeout=TIMEOUT)
            if r.status_code >= 400:
                print(f"  ! power attempt {n}: HTTP {r.status_code} — "
                      f"{r.text[:200]}", file=sys.stderr)
                continue

            payload = r.json()
            records = payload.get("records") or []
            if not records:
                print(f"  ! power attempt {n}: HTTP 200 but zero records. "
                      f"url={r.url} body={json.dumps(payload)[:250]}",
                      file=sys.stderr)
                continue

            hours = _parse_power(records)
            if not hours:
                print(f"  ! power attempt {n}: {len(records)} records but none "
                      f"parsable. First record: {json.dumps(records[0])[:250]}",
                      file=sys.stderr)
                continue

            hours.sort(key=lambda h: h["hour"])
            return {
                "hours": hours,
                "avg": round(sum(h["ore"] for h in hours) / len(hours), 1),
                "cheap": min(hours, key=lambda h: h["ore"]),
                "dear": max(hours, key=lambda h: h["ore"]),
            }
        except Exception as e:
            print(f"  ! power attempt {n} failed: {type(e).__name__}: {e}",
                  file=sys.stderr)

    return None


def _parse_power(records):
    """
    Pull hour and price out of the records without hardcoding column names —
    match on substring instead, so a renamed field doesn't kill the section.
    """
    sample = records[0]
    hour_key = next((k for k in sample if "hour" in k.lower()
                     or "time" in k.lower()), None)
    price_key = next((k for k in sample if "spotprice" in k.lower()
                      and "eur" not in k.lower()), None)
    if not price_key:
        price_key = next((k for k in sample if "price" in k.lower()), None)
    if not (hour_key and price_key):
        return []

    hours = []
    for rec in records:
        price, stamp = rec.get(price_key), str(rec.get(hour_key) or "")
        if price is None or len(stamp) < 16:
            continue
        # DKK per MWh -> øre per kWh.
        hours.append({"hour": stamp[11:16], "ore": round(float(price) / 10, 1)})
    return hours


def fetch_github_trending(limit=5):
    """Most-starred repos created in the last week. Uses the token we already have."""
    since = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    try:
        data = gh("GET", "/search/repositories"
                  f"?q=created:>{since}&sort=stars&order=desc&per_page={limit}")
        out = []
        for repo in data.get("items", [])[:limit]:
            desc = (repo.get("description") or "").strip()
            lang = repo.get("language")
            stars = repo.get("stargazers_count", 0)
            meta = f"{stars:,} stars this week"
            if lang:
                meta += f" · {lang}"
            out.append({
                "title": repo["full_name"],
                "summary": f"{desc} ({meta})" if desc else meta,
                "url": repo["html_url"],
                "image": None,
            })
        return out
    except Exception as e:
        print(f"  ! github trending failed: {e}", file=sys.stderr)
        return []


def fetch_hackernews(limit=5):
    """Top Hacker News stories. Open API, no key, but one request per story."""
    base = "https://hacker-news.firebaseio.com/v0"
    try:
        ids = requests.get(f"{base}/topstories.json", timeout=TIMEOUT).json()[:limit]
        out = []
        for sid in ids:
            s = requests.get(f"{base}/item/{sid}.json", timeout=TIMEOUT).json() or {}
            if not s.get("title"):
                continue
            out.append({
                "title": s["title"],
                "summary": f"{s.get('score', 0)} points · "
                           f"{s.get('descendants', 0)} comments",
                "url": s.get("url") or f"https://news.ycombinator.com/item?id={sid}",
                "image": None,
            })
        return out
    except Exception as e:
        print(f"  ! hacker news failed: {e}", file=sys.stderr)
        return []


def fetch_football(limit=6):
    """
    Yesterday's results and today's fixtures from football-data.org.

    The free tier covers 12 competitions — Premier League, La Liga, Bundesliga,
    Serie A, Ligue 1, Eredivisie, Primeira Liga, Championship, Brasileirão,
    Champions League, World Cup and the Euros. The Danish Superliga is NOT
    among them; that needs a paid plan or a different provider.

    Skipped silently when FOOTBALL_DATA_KEY isn't set.
    """
    key = os.environ.get("FOOTBALL_DATA_KEY")
    if not key:
        return []

    today = dt.datetime.now(TZ).date()
    yesterday = today - dt.timedelta(days=1)
    try:
        r = requests.get(
            "https://api.football-data.org/v4/matches",
            headers={"X-Auth-Token": key},
            params={"dateFrom": yesterday.isoformat(), "dateTo": today.isoformat()},
            timeout=TIMEOUT,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

        played, upcoming = [], []
        for m in r.json().get("matches", []):
            home = m.get("homeTeam", {}).get("shortName") \
                or m.get("homeTeam", {}).get("name", "?")
            away = m.get("awayTeam", {}).get("shortName") \
                or m.get("awayTeam", {}).get("name", "?")
            comp = m.get("competition", {}).get("name", "")
            status = m.get("status", "")
            # The free tier exposes no per-match page, so these stay unlinked.
            url = ""

            if status == "FINISHED":
                ft = m.get("score", {}).get("fullTime", {})
                h, a = ft.get("home"), ft.get("away")
                if h is None or a is None:
                    continue
                played.append({
                    "title": f"{home} {h}–{a} {away}",
                    "summary": f"{comp} · full time",
                    "url": url, "image": None,
                })
            elif status in ("TIMED", "SCHEDULED"):
                when = m.get("utcDate", "")
                try:
                    local = dt.datetime.fromisoformat(
                        when.replace("Z", "+00:00")).astimezone(TZ)
                    if local.date() != today:
                        continue
                    clock = f"{local:%H:%M}"
                except Exception:
                    clock = "today"
                upcoming.append({
                    "title": f"{home} v {away}",
                    "summary": f"{comp} · kicks off {clock}",
                    "url": url, "image": None,
                })

        # Today's fixtures first — they're the actionable ones.
        return (upcoming + played)[:limit]
    except Exception as e:
        print(f"  ! football failed: {e}", file=sys.stderr)
        return []


def fetch_superliga(limit=4):
    """
    Danish Superliga fixtures and results via Sportmonks, whose free plan
    covers exactly the Superliga and the Scottish Premiership.

    Uses the fixture's own `name` and `result_info` strings rather than
    unpicking the scores array, which is both simpler and less likely to break
    if their response shape shifts.

    Skipped silently when SPORTMONKS_KEY isn't set.
    """
    key = os.environ.get("SPORTMONKS_KEY")
    if not key:
        return []

    today = dt.datetime.now(TZ).date()
    yesterday = today - dt.timedelta(days=1)
    try:
        r = requests.get(
            "https://api.sportmonks.com/v3/football/fixtures/between/"
            f"{yesterday}/{today}",
            params={"api_token": key},
            timeout=TIMEOUT,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

        played, upcoming = [], []
        for fx in r.json().get("data", []) or []:
            name = (fx.get("name") or "").replace(" vs ", " v ").strip()
            if not name:
                continue
            result = (fx.get("result_info") or "").strip()

            if result:
                played.append({
                    "title": name, "summary": result, "url": "", "image": None,
                })
                continue

            clock = ""
            starts = fx.get("starting_at") or ""
            try:
                # Sportmonks returns "YYYY-MM-DD HH:MM:SS" in UTC.
                stamp = dt.datetime.strptime(starts, "%Y-%m-%d %H:%M:%S")
                local = stamp.replace(tzinfo=dt.timezone.utc).astimezone(TZ)
                if local.date() != today:
                    continue
                clock = f"kicks off {local:%H:%M}"
            except Exception:
                clock = "today"
            upcoming.append({
                "title": name, "summary": f"Superliga · {clock}",
                "url": "", "image": None,
            })

        return (upcoming + played)[:limit]
    except Exception as e:
        print(f"  ! superliga failed: {e}", file=sys.stderr)
        return []


def fetch_music(limit=5, country="dk"):
    """
    Most-played songs from Apple's marketing RSS feed. No key, no signup,
    updated daily. Apple publishes this per country — there is no global
    chart, so this is Denmark only.
    """
    try:
        r = requests.get(
            f"https://rss.marketingtools.apple.com/api/v2/{country}/music/"
            f"most-played/{limit}/songs.json",
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        out = []
        for song in r.json().get("feed", {}).get("results", [])[:limit]:
            art = song.get("artworkUrl100") or None
            out.append({
                "title": song.get("name", "").strip(),
                "summary": song.get("artistName", "").strip(),
                "url": song.get("url", ""),
                "image": art,
            })
        return out
    except Exception as e:
        print(f"  ! music failed: {e}", file=sys.stderr)
        return []


# Tune these to change which jobs show up.
JOB_QUERY = "software developer engineer udvikler programmør"
JOB_WHERE = "København"
JOB_MAX_AGE_DAYS = 3


def fetch_jobs(limit=5):
    """
    Recent software roles near Copenhagen via Adzuna.
    Skipped silently unless both ADZUNA_APP_ID and ADZUNA_APP_KEY are set.
    """
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []

    try:
        r = requests.get(
            "https://api.adzuna.com/v1/api/jobs/dk/search/1",
            params={
                "app_id": app_id,
                "app_key": app_key,
                "what_or": JOB_QUERY,
                "where": JOB_WHERE,
                "max_days_old": JOB_MAX_AGE_DAYS,
                "results_per_page": limit,
                "sort_by": "date",
                "content-type": "application/json",
            },
            timeout=TIMEOUT,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

        out = []
        for job in r.json().get("results", [])[:limit]:
            company = (job.get("company") or {}).get("display_name", "").strip()
            where = (job.get("location") or {}).get("display_name", "").strip()
            bits = [b for b in (company, where) if b]

            lo, hi = job.get("salary_min"), job.get("salary_max")
            if lo and hi:
                bits.append(f"{int(lo):,}–{int(hi):,} kr".replace(",", "."))

            out.append({
                "title": (job.get("title") or "").strip(),
                "summary": " · ".join(bits),
                "url": job.get("redirect_url", ""),
                "image": None,
            })
        return out
    except Exception as e:
        print(f"  ! jobs failed: {e}", file=sys.stderr)
        return []


# --------------------------------------------------------------- optional AI


def summarize(sections):
    """
    Rewrite headlines into tight one-liners and pick the most significant
    stories rather than merely the most recent. Returns the input unchanged
    if no key is set or the call fails.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return sections

    # Manual runs are for checking feeds and layout, not prose — and every
    # call costs money. SKIP_AI keeps testing free.
    if os.environ.get("SKIP_AI") == "1":
        print("  (skipping AI summaries — SKIP_AI is set)")
        return sections

    payload = {
        name: [{"title": i["title"],
                "summary": i["summary"][:AI_SUMMARY_CHARS]} for i in items]
        for name, items in sections.items()
    }
    prompt = (
        "Below are today's raw RSS items grouped by section. For each section, "
        f"pick the {ITEMS_PER_SECTION} most significant stories and rewrite each as:\n"
        '  {"title": "<short punchy headline, max 8 words>", '
        '"line": "<one sentence of context, max 25 words>"}\n\n'
        "Write in English even where the source is Danish. Be factual and dry — "
        "no hype, no editorialising. For Games, favour actual releases over "
        "industry news. Respond with JSON only: an object mapping each section "
        "name to its array. No markdown fences.\n\n"
        + json.dumps(payload, ensure_ascii=False)[:60000]
    )

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": AI_MODEL,
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")

        data = r.json()

        # The response may contain several content blocks and they are not all
        # text, so pick out the text ones rather than assuming the first block.
        text = "".join(
            b.get("text", "") for b in data.get("content", [])
            if b.get("type") == "text"
        ).strip()

        if not text:
            kinds = [b.get("type") for b in data.get("content", [])]
            raise RuntimeError(
                f"no text in response; blocks={kinds} "
                f"stop_reason={data.get('stop_reason')}"
            )
        if data.get("stop_reason") == "max_tokens":
            raise RuntimeError("response hit max_tokens and was cut off mid-JSON")

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
            text = text.strip()

        rewritten = json.loads(text)
        if not isinstance(rewritten, dict):
            raise RuntimeError(f"expected a JSON object, got {type(rewritten).__name__}")
    except Exception as e:
        print(f"  ! AI summarize failed, using raw headlines: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return sections

    out = {}
    for name, items in sections.items():
        new = rewritten.get(name)
        if not new:
            out[name] = items
            continue
        merged = []
        for idx, entry in enumerate(new[:ITEMS_PER_SECTION]):
            fallback = items[idx] if idx < len(items) else {}
            source = _match_item(entry.get("title", ""), items) or fallback
            merged.append({
                "title": entry.get("title") or source.get("title", ""),
                "summary": entry.get("line", ""),
                "url": source.get("url", ""),
                "image": source.get("image"),
            })
        out[name] = merged
    return out


def _match_item(title, items):
    """
    Best-effort: match a rewritten headline back to the article it came from,
    so we keep the original link and thumbnail.
    """
    words = {w for w in title.lower().split() if len(w) > 4}
    best, score = None, 0
    for i in items:
        overlap = len(words & {w for w in i["title"].lower().split() if len(w) > 4})
        if overlap > score:
            best, score = i, overlap
    return best


# ------------------------------------------------------------- render: page


CSS = """
:root{
  color-scheme:light dark;
  --bg:#f4f4f1; --fg:#17171a; --dim:#6e6e78; --card:#fff;
  --line:#e6e6e0; --shadow:0 1px 3px rgba(0,0,0,.05),0 8px 24px rgba(0,0,0,.04);
}
@media(prefers-color-scheme:dark){
  :root{--bg:#101014; --fg:#ececef; --dim:#8e8e9a; --card:#1a1a20;
        --line:#2a2a33; --shadow:0 1px 3px rgba(0,0,0,.4);}
}
*{box-sizing:border-box}
body{margin:0;padding:0 20px 64px;background:var(--bg);color:var(--fg);
  font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin-inline:auto}

/* ---- masthead ---- */
header{padding:36px 0 22px}
h1{font-size:clamp(28px,4vw,40px);line-height:1.05;margin:0;
  letter-spacing:-.03em;font-weight:700}
.date{font-size:14px;color:var(--dim);margin:6px 0 0;
  text-transform:uppercase;letter-spacing:.09em;font-weight:600}

/* ---- weather hero ---- */
.hero{border-radius:20px;padding:26px 28px;color:#fff;margin-bottom:32px;
  display:flex;align-items:center;gap:24px;flex-wrap:wrap;
  box-shadow:var(--shadow)}
.hero-emoji{font-size:clamp(52px,7vw,76px);line-height:1;
  filter:drop-shadow(0 2px 6px rgba(0,0,0,.25))}
.hero-main{flex:1 1 220px}
.hero-temp{font-size:clamp(44px,6vw,62px);font-weight:700;letter-spacing:-.04em;
  line-height:1}
.hero-cond{font-size:19px;font-weight:600;opacity:.95;margin-top:2px}
.hero-stats{display:flex;gap:26px;flex-wrap:wrap;
  border-left:1px solid rgba(255,255,255,.28);padding-left:26px}
.stat-v{font-size:20px;font-weight:700;letter-spacing:-.01em}
.stat-l{font-size:11px;text-transform:uppercase;letter-spacing:.08em;
  opacity:.8;margin-top:2px}
@media(max-width:640px){
  .hero-stats{border-left:none;padding-left:0;gap:20px;
    border-top:1px solid rgba(255,255,255,.28);padding-top:14px;flex-basis:100%}
}

/* ---- section grid: wide on desktop, single column on phone ---- */
.grid{display:grid;gap:22px;
  grid-template-columns:repeat(auto-fit,minmax(330px,1fr));
  align-items:start}
.sec{background:var(--card);border:1px solid var(--line);border-radius:18px;
  padding:20px 22px 8px;box-shadow:var(--shadow)}
.sec h2{display:flex;align-items:center;gap:9px;margin:0 0 14px;
  font-size:12px;text-transform:uppercase;letter-spacing:.11em;font-weight:700}
.dot{width:9px;height:9px;border-radius:50%;flex:none}
.sec-emoji{font-size:16px}

ol{list-style:none;margin:0;padding:0;counter-reset:n}
li{display:flex;gap:14px;padding:15px 0;border-top:1px solid var(--line)}
li:first-child{border-top:none;padding-top:2px}
.rank{counter-increment:n;font-size:12px;font-weight:700;color:var(--dim);
  min-width:15px;padding-top:2px;font-variant-numeric:tabular-nums}
.rank::before{content:counter(n)}
.body{flex:1;min-width:0}
.t{font-size:16px;font-weight:650;letter-spacing:-.01em;display:block;
  line-height:1.32}
.d{font-size:14px;color:var(--dim);margin-top:4px;line-height:1.45}
.thumb{width:74px;height:74px;border-radius:11px;object-fit:cover;flex:none;
  background:var(--line)}
li.lead{display:block}
li.lead .lead-img{width:100%;height:172px;object-fit:cover;border-radius:12px;
  margin-bottom:11px;background:var(--line);display:block}
li.lead .t{font-size:19px;line-height:1.25}
a{color:inherit;text-decoration:none}
a:hover .t{text-decoration:underline;text-underline-offset:2px}

/* ---- filter chips ---- */
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 24px}
.chip{font:inherit;font-size:13px;font-weight:600;padding:7px 14px;
  border-radius:999px;border:1px solid var(--line);background:var(--card);
  color:var(--dim);cursor:pointer;transition:.12s;white-space:nowrap}
.chip:hover{border-color:var(--dim)}
.chip[aria-pressed="true"]{background:var(--fg);color:var(--bg);
  border-color:var(--fg)}
.sec[hidden]{display:none}

/* ---- electricity ---- */
.power{background:var(--card);border:1px solid var(--line);border-radius:18px;
  padding:20px 22px;margin-bottom:22px;box-shadow:var(--shadow)}
.power h2{display:flex;align-items:center;gap:9px;margin:0 0 16px;
  font-size:12px;text-transform:uppercase;letter-spacing:.11em;font-weight:700}
.pgrid{display:flex;gap:30px;flex-wrap:wrap;margin-bottom:18px}
.pv{font-size:23px;font-weight:700;letter-spacing:-.02em}
.pv small{font-size:13px;font-weight:600;color:var(--dim);margin-left:3px}
.pl{font-size:11px;text-transform:uppercase;letter-spacing:.08em;
  color:var(--dim);margin-top:3px}
.cheap{color:#1f9d55}.dear{color:#d64545}
.bars{display:flex;align-items:flex-end;gap:2px;height:56px}
.bar{flex:1;border-radius:3px 3px 0 0;background:var(--line);min-height:3px}
.bar.b-cheap{background:#1f9d55}.bar.b-dear{background:#d64545}
.axis{display:flex;justify-content:space-between;font-size:10px;
  color:var(--dim);margin-top:5px;font-variant-numeric:tabular-nums}
.note{font-size:11px;color:var(--dim);margin-top:12px;line-height:1.4}

footer{margin-top:40px;text-align:center;font-size:12px;color:var(--dim)}

/* On phones, stop stretching edge to edge and centre a readable column. */
@media(max-width:700px){
  body{padding:0 16px 48px}
  .wrap{max-width:580px}
  .grid{grid-template-columns:1fr;gap:18px}
  .hero{padding:22px}
}
"""


def render_power(power):
    """Electricity card: headline numbers plus a 24-bar price curve."""
    if not power:
        return []
    lo = power["cheap"]["ore"]
    hi = power["dear"]["ore"]
    span = max(hi - lo, 0.1)

    bars = []
    for h in power["hours"]:
        height = 12 + 88 * (h["ore"] - lo) / span
        cls = ("bar b-cheap" if h is power["cheap"]
               else "bar b-dear" if h is power["dear"] else "bar")
        bars.append(
            f"<div class='{cls}' style='height:{height:.0f}%' "
            f"title='{h['hour']} — {h['ore']} øre'></div>"
        )

    emoji, colour = SECTION_STYLE["Power"]
    return [
        "<section class='power' data-sec='Power'>",
        f"<h2><span class='dot' style='background:{colour}'></span>"
        f"<span class='sec-emoji'>{emoji}</span>Electricity</h2>",
        "<div class='pgrid'>",
        f"<div><div class='pv'>{power['avg']}<small>øre</small></div>"
        "<div class='pl'>Average</div></div>",
        f"<div><div class='pv cheap'>{lo}<small>øre</small></div>"
        f"<div class='pl'>Cheapest · {power['cheap']['hour']}</div></div>",
        f"<div><div class='pv dear'>{hi}<small>øre</small></div>"
        f"<div class='pl'>Priciest · {power['dear']['hour']}</div></div>",
        "</div>",
        "<div class='bars'>" + "".join(bars) + "</div>",
        f"<div class='axis'><span>{power['hours'][0]['hour']}</span>"
        f"<span>{power['hours'][len(power['hours']) // 2]['hour']}</span>"
        f"<span>{power['hours'][-1]['hour']}</span></div>",
        "<div class='note'>Spot price for DK2, øre per kWh. Excludes transport "
        "tariffs, taxes and VAT — useful for comparing hours, not for "
        "predicting your bill.</div>",
        "</section>",
    ]


def render_html(weather, sections, today, power=None):
    esc = html.escape
    tint = hero_tint(weather["code"]) if weather else HERO_TINT["cloud"]
    icon = weather["emoji"] if weather else "📰"

    p = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<meta name='apple-mobile-web-app-capable' content='yes'>",
        "<meta name='apple-mobile-web-app-title' content='Brief'>",
        f"<meta name='theme-color' content='{tint[1]}'>",
        # Emoji favicon, so the browser tab and bookmark aren't a blank page.
        "<link rel='icon' href=\"data:image/svg+xml,"
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
        f"<text y='.9em' font-size='90'>{icon}</text></svg>\">",
        f"<title>Daily Brief · {today:%-d %b}</title>",
        f"<style>{CSS}</style></head><body><div class='wrap'>",
        "<header><h1>Daily Brief</h1>",
        f"<p class='date'>{today.strftime('%A %-d %B')} · {CITY}</p></header>",
    ]

    if weather:
        w = weather
        p += [
            f"<div class='hero' style=\"background:linear-gradient(135deg,"
            f"{tint[0]},{tint[1]})\">",
            f"<div class='hero-emoji'>{w['emoji']}</div>",
            "<div class='hero-main'>",
            f"<div class='hero-temp'>{w['high']}°</div>",
            f"<div class='hero-cond'>{esc(w['cond'])}</div></div>",
            "<div class='hero-stats'>",
            f"<div><div class='stat-v'>{w['low']}°</div>"
            "<div class='stat-l'>Low</div></div>",
            f"<div><div class='stat-v'>{w['rain']}%</div>"
            "<div class='stat-l'>Rain</div></div>",
            f"<div><div class='stat-v'>{w['wind']}</div>"
            "<div class='stat-l'>km/h</div></div>",
            f"<div><div class='stat-v'>{w['sunset']}</div>"
            "<div class='stat-l'>Sunset</div></div>",
            "</div></div>",
        ]

    # Filter chips — every section that actually has content today.
    names = (["Power"] if power else []) + [n for n, i in sections.items() if i]
    if names:
        p.append("<div class='chips' role='group' aria-label='Filter sections'>")
        p.append("<button class='chip' data-all aria-pressed='true'>All</button>")
        for n in names:
            emoji, _ = SECTION_STYLE.get(n, ("•", "#888"))
            p.append(f"<button class='chip' data-target='{esc(n)}' "
                     f"aria-pressed='true'>{emoji} {esc(n)}</button>")
        p.append("</div>")

    p += render_power(power)

    p.append("<div class='grid'>")
    for name, items in sections.items():
        if not items:
            continue
        emoji, colour = SECTION_STYLE.get(name, ("•", "#888"))
        p += [
            f"<section class='sec' data-sec='{esc(name)}'>",
            f"<h2><span class='dot' style='background:{colour}'></span>"
            f"<span class='sec-emoji'>{emoji}</span>{esc(name)}</h2><ol>",
        ]
        for idx, i in enumerate(items):
            img = i.get("image")
            link = esc(i.get("url") or "")
            title = esc(i["title"])
            summary = esc(i.get("summary") or "")

            # The first story in each section gets a full-width lead image.
            if idx == 0 and img:
                p.append("<li class='lead'>")
                p.append(f"<a href='{link}'>" if link else "<div>")
                p.append(f"<img class='lead-img' src='{esc(img)}' alt='' "
                         "loading='lazy' referrerpolicy='no-referrer'>")
                p.append(f"<span class='t'>{title}</span>")
                if summary:
                    p.append(f"<span class='d'>{summary}</span>")
                p.append("</a>" if link else "</div>")
                p.append("</li>")
                continue

            p.append("<li><span class='rank'></span>")
            p.append(f"<a href='{link}' class='body'>" if link
                     else "<div class='body'>")
            p.append(f"<span class='t'>{title}</span>")
            if summary:
                p.append(f"<span class='d'>{summary}</span>")
            p.append("</a>" if link else "</div>")
            if img:
                p.append(f"<img class='thumb' src='{esc(img)}' alt='' "
                         "loading='lazy' referrerpolicy='no-referrer'>")
            p.append("</li>")
        p.append("</ol></section>")
    p.append("</div>")

    p.append(f"<footer>Updated {today:%H:%M} · {CITY}</footer>")
    p.append("</div>")

    # Section filtering. Runs after load, remembers your choice, and degrades
    # to showing everything if JavaScript is off.
    p.append("""<script>
(function(){
  var KEY='brief-hidden';
  var chips=[].slice.call(document.querySelectorAll('.chip[data-target]'));
  var all=document.querySelector('.chip[data-all]');
  if(!chips.length) return;
  var hidden;
  try{ hidden=JSON.parse(localStorage.getItem(KEY))||[]; }catch(e){ hidden=[]; }

  function apply(){
    chips.forEach(function(c){
      var name=c.dataset.target, off=hidden.indexOf(name)>-1;
      c.setAttribute('aria-pressed', off?'false':'true');
      document.querySelectorAll("[data-sec='"+name+"']").forEach(function(s){
        s.hidden=off;
      });
    });
    if(all) all.setAttribute('aria-pressed', hidden.length?'false':'true');
    try{ localStorage.setItem(KEY, JSON.stringify(hidden)); }catch(e){}
  }

  chips.forEach(function(c){
    c.addEventListener('click', function(){
      var n=c.dataset.target, i=hidden.indexOf(n);
      if(i>-1) hidden.splice(i,1); else hidden.push(n);
      apply();
    });
  });
  if(all) all.addEventListener('click', function(){ hidden=[]; apply(); });
  apply();
})();
</script>""")
    p.append("</body></html>")
    return "".join(p)


# --------------------------------------------------------- render: markdown


def render_markdown(weather, sections, page_url, power=None):
    """GitHub strips HTML from issues, so the notification body is markdown."""
    lines = []
    if weather:
        w = weather
        lines += [
            f"## {w['emoji']} {w['high']}° · {w['cond']}",
            f"Low {w['low']}° · {w['rain']}% rain · wind {w['wind']} km/h · "
            f"sunset {w['sunset']}",
            "",
        ]

    if power:
        lines += [
            f"{SECTION_STYLE['Power'][0]} **Power** "
            f"{power['avg']} øre/kWh average · "
            f"cheapest {power['cheap']['ore']} at {power['cheap']['hour']} · "
            f"priciest {power['dear']['ore']} at {power['dear']['hour']}",
            "",
        ]

    for name, items in sections.items():
        if not items:
            continue
        emoji, _ = SECTION_STYLE.get(name, ("•", ""))
        lines.append(f"### {emoji} {name}")
        for i in items:
            title = i["title"]
            link = i.get("url")
            lines.append(f"- **[{title}]({link})**" if link else f"- **{title}**")
            if i.get("summary"):
                lines.append(f"  {i['summary']}")
        lines.append("")

    if page_url:
        lines.append(f"[Open the full page]({page_url})")
    return "\n".join(lines)


# ------------------------------------------------------------------- github


def gh(method, path, **kw):
    token = os.environ["GITHUB_TOKEN"]
    r = requests.request(
        method, f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30, **kw,
    )
    r.raise_for_status()
    return r.json() if r.text else {}


def post_issue(repo, title, body):
    """Open today's issue and close yesterday's so the list stays short."""
    for old in gh("GET", f"/repos/{repo}/issues?labels=brief&state=open&per_page=20"):
        gh("PATCH", f"/repos/{repo}/issues/{old['number']}", json={"state": "closed"})
        print(f"  closed #{old['number']}")

    issue = gh("POST", f"/repos/{repo}/issues",
               json={"title": title, "body": body, "labels": ["brief"]})
    print(f"  opened #{issue['number']}")


def pages_url(repo):
    owner, name = repo.split("/", 1)
    return f"https://{owner}.github.io/{name}/"


# ------------------------------------------------------------------- main


def main():
    now = dt.datetime.now(TZ)

    # GitHub cron is UTC and ignores daylight saving, so the workflow fires at
    # two UTC times and we keep only the one that is 07:00 in Copenhagen.
    if os.environ.get("FORCE_RUN") != "1" and now.hour != SEND_HOUR:
        print(f"Local time is {now:%H:%M} in {CITY}, not 0{SEND_HOUR}:00 — skipping.")
        return

    print(f"Building brief for {now:%Y-%m-%d %H:%M %Z}")

    weather = fetch_weather()
    sections = {}
    use_ai = bool(os.environ.get("ANTHROPIC_API_KEY")) \
        and os.environ.get("SKIP_AI") != "1"
    for name, urls in FEEDS.items():
        # Over-fetch so the AI has a real choice — but only when it will run,
        # since every extra candidate is input tokens you pay for.
        sections[name] = fetch_section(
            urls, ITEMS_PER_SECTION * AI_OVERFETCH if use_ai else ITEMS_PER_SECTION)
        print(f"  {name}: {len(sections[name])} items")

    # Only the RSS sections go through the AI — the rest arrive clean already.
    sections = summarize(sections)
    for name in sections:
        sections[name] = sections[name][:ITEMS_PER_SECTION]

    sections["GitHub"] = fetch_github_trending()
    print(f"  GitHub: {len(sections['GitHub'])} repos")
    sections["Hacker News"] = fetch_hackernews()
    print(f"  Hacker News: {len(sections['Hacker News'])} stories")

    # These skip themselves when their keys aren't set.
    sections["Superliga"] = fetch_superliga()
    print(f"  Superliga: {len(sections['Superliga'])} matches")
    sections["Football"] = fetch_football()
    print(f"  Football: {len(sections['Football'])} matches")
    sections["Music"] = fetch_music()
    print(f"  Music: {len(sections['Music'])} tracks")
    sections["Jobs"] = fetch_jobs()
    print(f"  Jobs: {len(sections['Jobs'])} listings")

    power = fetch_power()
    print(f"  Power: {'ok' if power else 'unavailable'}")

    # An empty brief is worse than none — fail loudly so GitHub tells you.
    if not weather and not power and not any(sections.values()):
        sys.exit("Every source failed — publishing nothing. Check the log above.")

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    url = pages_url(repo) if repo else ""

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(render_html(weather, sections, now, power))
    print("  wrote docs/index.html")

    body = render_markdown(weather, sections, url, power)
    temp = f" · {weather['high']}°" if weather else ""
    title = f"Daily Brief · {now:%a %-d %b}{temp}"

    if os.environ.get("DRY_RUN") == "1":
        with open("issue-preview.md", "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n{body}")
        print("  dry run — wrote issue-preview.md, posted nothing")
        return

    post_issue(repo, title, body)


if __name__ == "__main__":
    main()
