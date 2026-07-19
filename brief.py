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


# ---------------------------------------------------------------- weather

WEATHER_CODES = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 80: "Showers",
    81: "Showers", 82: "Heavy showers", 95: "Thunderstorms",
    96: "Thunderstorms with hail", 99: "Severe thunderstorms",
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
        return {
            "high": round(d["temperature_2m_max"][0]),
            "low": round(d["temperature_2m_min"][0]),
            "rain": d["precipitation_probability_max"][0],
            "wind": round(d["wind_speed_10m_max"][0]),
            "cond": WEATHER_CODES.get(d["weather_code"][0], "—"),
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

    payload = {
        name: [{"title": i["title"], "summary": i["summary"]} for i in items]
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
                "model": "claude-sonnet-5",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        rewritten = json.loads(text)
    except Exception as e:
        print(f"  ! AI summarize failed, using raw headlines: {e}", file=sys.stderr)
        return sections

    out = {}
    for name, items in sections.items():
        new = rewritten.get(name)
        if not new:
            out[name] = items
            continue
        merged = []
        for idx, entry in enumerate(new[:ITEMS_PER_SECTION]):
            original = items[idx] if idx < len(items) else {}
            merged.append({
                "title": entry.get("title", original.get("title", "")),
                "summary": entry.get("line", ""),
                "url": _match_url(entry.get("title", ""), items) or original.get("url", ""),
            })
        out[name] = merged
    return out


def _match_url(title, items):
    """Best-effort: link a rewritten headline back to its source article."""
    words = {w for w in title.lower().split() if len(w) > 4}
    best, score = None, 0
    for i in items:
        overlap = len(words & {w for w in i["title"].lower().split() if len(w) > 4})
        if overlap > score:
            best, score = i["url"], overlap
    return best


# ------------------------------------------------------------- render: page


CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{margin:0;padding:24px 20px 48px;font:16px/1.5 -apple-system,BlinkMacSystemFont,
"Segoe UI",system-ui,sans-serif;max-width:560px;margin-inline:auto;
background:#fbfbfa;color:#1a1a18}
h1{font-size:20px;margin:0 0 2px;letter-spacing:-.01em}
.meta{font-size:13px;color:#77776f;margin:0 0 24px}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#77776f;
margin:28px 0 10px;font-weight:600}
.card{background:#fff;border:1px solid #e8e8e3;border-radius:12px;padding:14px 16px}
.temp{font-size:30px;font-weight:600;letter-spacing:-.02em}
.cond{font-size:15px;margin-left:10px}
.wdetail{font-size:13px;color:#77776f;margin-top:8px}
ul{list-style:none;margin:0;padding:0}
li{padding:11px 0;border-bottom:1px solid #eeeee8}
li:first-child{padding-top:0}
li:last-child{border-bottom:none;padding-bottom:0}
.t{font-weight:600;display:block;margin-bottom:2px}
.d{font-size:14px;color:#5a5a54}
a{color:#1a5fb4;text-decoration:none}
footer{margin-top:32px;font-size:12px;color:#9a9a92;text-align:center}
@media(prefers-color-scheme:dark){
body{background:#16161a;color:#e8e8e6}
.card{background:#1f1f24;border-color:#2e2e35}
li{border-color:#2a2a31}
.d{color:#9a9aa2}
a{color:#8ab4f8}
}
"""


def render_html(weather, sections, today):
    esc = html.escape
    p = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<meta name='apple-mobile-web-app-capable' content='yes'>",
        "<meta name='apple-mobile-web-app-title' content='Brief'>",
        f"<title>Daily Brief · {today:%-d %b}</title>",
        f"<style>{CSS}</style></head><body>",
        "<h1>Daily Brief</h1>",
        f"<p class='meta'>{today.strftime('%A, %-d %B %Y')} · {CITY}</p>",
    ]

    if weather:
        w = weather
        p += [
            "<h2>Weather</h2><div class='card'>",
            f"<span class='temp'>{w['high']}°</span>"
            f"<span class='cond'>{esc(w['cond'])}</span>",
            f"<div class='wdetail'>Low {w['low']}° · {w['rain']}% chance of rain · "
            f"wind {w['wind']} km/h<br>"
            f"Sunrise {w['sunrise']} · Sunset {w['sunset']}</div></div>",
        ]

    for name, items in sections.items():
        if not items:
            continue
        p.append(f"<h2>{esc(name)}</h2><div class='card'><ul>")
        for i in items:
            title = esc(i["title"])
            link = i.get("url")
            head = f"<a href='{esc(link)}'>{title}</a>" if link else title
            p.append(f"<li><span class='t'>{head}</span>")
            if i.get("summary"):
                p.append(f"<span class='d'>{esc(i['summary'])}</span>")
            p.append("</li>")
        p.append("</ul></div>")

    p.append(f"<footer>Updated {today:%H:%M}</footer></body></html>")
    return "".join(p)


# --------------------------------------------------------- render: markdown


def render_markdown(weather, sections, page_url):
    """GitHub strips HTML from issues, so the notification body is markdown."""
    lines = []
    if weather:
        w = weather
        lines += [
            f"**{w['high']}°** · {w['cond']} · low {w['low']}° · "
            f"{w['rain']}% rain · wind {w['wind']} km/h",
            f"<sub>Sunrise {w['sunrise']} · Sunset {w['sunset']}</sub>",
            "",
        ]

    for name, items in sections.items():
        if not items:
            continue
        lines.append(f"### {name}")
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
    use_ai = bool(os.environ.get("ANTHROPIC_API_KEY"))
    for name, urls in FEEDS.items():
        # Over-fetch so the AI has a real choice of stories to pick from.
        sections[name] = fetch_section(urls, ITEMS_PER_SECTION * 4 if use_ai else ITEMS_PER_SECTION)
        print(f"  {name}: {len(sections[name])} items")

    sections = summarize(sections)
    for name in sections:
        sections[name] = sections[name][:ITEMS_PER_SECTION]

    # An empty brief is worse than none — fail loudly so GitHub tells you.
    if not weather and not any(sections.values()):
        sys.exit("Every source failed — publishing nothing. Check the log above.")

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    url = pages_url(repo) if repo else ""

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(render_html(weather, sections, now))
    print("  wrote docs/index.html")

    body = render_markdown(weather, sections, url)
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
