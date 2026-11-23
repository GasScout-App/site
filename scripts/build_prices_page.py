#!/usr/bin/env python3
"""Builds gas-prices/index.html: today's gas prices around Princeton, NJ.

Runs on a schedule in GitHub Actions (.github/workflows/update-prices.yml),
after the app's daily price sync, and commits the page when it changes. The
page exists to be found on Google by people searching for gas prices near
Princeton, and to send them to the app.

Reads the same public data the app reads, with the same public key, so hidden
reports are already filtered out by the database. Needs SUPABASE_URL and
SUPABASE_ANON_KEY in the environment. Standard library only.

Usage: python3 scripts/build_prices_page.py   (from the site folder)
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SITE_URL = "https://gasscout-app.github.io/site/"
PAGE_URL = SITE_URL + "gas-prices/"
APP_STORE = "https://apps.apple.com/app/id6813200661"
PLAY_STORE = "https://play.google.com/store/apps/details?id=com.junctionlabs.gasscout"
EASTERN = ZoneInfo("America/New_York")

# A price older than this isn't ranked as "today's" cheapest; it still shows
# in the by-town list, marked with its age.
FRESH_DAYS = 7

GRADES = [
    ("regular", "Regular"),
    ("mid-grade", "Mid-grade"),
    ("premium", "Premium"),
    ("diesel", "Diesel"),
]
MEMBERS_ONLY = re.compile(r"\b(costco|sam['’]?s club|bj['’]?s)\b", re.I)


def fetch_stations() -> list[dict]:
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_ANON_KEY"]
    url = (
        base
        + "/rest/v1/stations?select=id,name,brand,address,latitude,longitude,"
        + "price_reports(price,fuel_type,reported_at,source)"
    )
    request = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def parse_time(value: str) -> datetime:
    # PostgREST timestamps can carry 1-6 fractional digits; fromisoformat on
    # older Pythons wants exactly 3 or 6.
    value = re.sub(r"\.(\d+)", lambda m: "." + m.group(1).ljust(6, "0")[:6], value)
    return datetime.fromisoformat(value)


def town_of(address: str | None) -> str:
    # "4100 Quakerbridge Rd #1199b, Lawrence Township, NJ 08648, USA"
    if not address:
        return "Other"
    parts = [p.strip() for p in address.split(",")]
    for i, part in enumerate(parts):
        if part.startswith("NJ") and i > 0:
            return parts[i - 1]
    return "Other"


def street_of(address: str | None) -> str:
    return (address or "").split(",")[0].strip()


def age_text(at: datetime, now: datetime) -> str:
    hours = (now - at).total_seconds() / 3600
    if hours < 1:
        return "under an hour ago"
    if hours < 24:
        return f"{int(hours)}h ago"
    days = int(hours // 24)
    return f"{days} day{'s' if days != 1 else ''} ago"


def newest_prices(stations: list[dict]) -> list[dict]:
    """One row per station and grade: the newest report, as the app shows it."""
    rows = []
    for station in stations:
        newest: dict[str, dict] = {}
        for report in station.get("price_reports") or []:
            grade = report["fuel_type"]
            at = parse_time(report["reported_at"])
            if grade not in newest or at > newest[grade]["at"]:
                newest[grade] = {"price": float(report["price"]), "at": at, "source": report["source"]}
        for grade, report in newest.items():
            label = station.get("brand") or station["name"]
            rows.append(
                {
                    "grade": grade,
                    "price": report["price"],
                    "at": report["at"],
                    "by_driver": report["source"] == "user",
                    "name": label,
                    "street": street_of(station.get("address")),
                    "town": town_of(station.get("address")),
                    "members_only": bool(
                        MEMBERS_ONLY.search(station["name"] or "")
                        or MEMBERS_ONLY.search(station.get("brand") or "")
                    ),
                }
            )
    return rows


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def price_row(row: dict, now: datetime, rank: int | None = None) -> str:
    badges = ""
    if row["members_only"]:
        badges += '<span class="badge members">Members only</span>'
    source = "reported by a driver" if row["by_driver"] else "updated"
    stale = (now - row["at"]) > timedelta(hours=24)
    rank_cell = f'<td class="rank">{rank}</td>' if rank is not None else ""
    return (
        f"<tr>{rank_cell}"
        f'<td><div class="station">{esc(row["name"])} {badges}</div>'
        f'<div class="where">{esc(row["street"])}, {esc(row["town"])}</div>'
        f'<div class="age{" stale" if stale else ""}">{source} {age_text(row["at"], now)}</div></td>'
        f'<td class="price">${row["price"]:.2f}</td></tr>'
    )


def build(stations: list[dict], now: datetime) -> str:
    rows = newest_prices(stations)
    fresh = [r for r in rows if now - r["at"] <= timedelta(days=FRESH_DAYS)]
    station_count = len(stations)
    priced_count = len({(r["name"], r["street"]) for r in fresh})
    updated = now.astimezone(EASTERN)
    updated_text = updated.strftime("%A, %B %-d, %Y at %-I:%M %p ET")
    date_text = updated.strftime("%B %-d, %Y")

    by_grade = {g: sorted((r for r in fresh if r["grade"] == g), key=lambda r: r["price"]) for g, _ in GRADES}
    # "Cheapest" means cheapest anyone can buy: a members-only club price is
    # listed with its badge but doesn't take the headline.
    def cheapest_open(grade: str) -> dict | None:
        return next((r for r in by_grade[grade] if not r["members_only"]), None)

    best_regular = cheapest_open("regular")
    regular = by_grade["regular"]
    spread = (
        regular[-1]["price"] - best_regular["price"]
        if best_regular and len(regular) > 1
        else None
    )

    if best_regular:
        description = (
            f"Cheapest regular gas near Princeton, NJ today: ${best_regular['price']:.2f} at "
            f"{best_regular['name']}, {best_regular['street']}. Prices at {priced_count} stations "
            f"within 10 miles, updated daily."
        )
    else:
        description = f"Today's gas prices at {station_count} stations within 10 miles of Princeton, NJ."

    cards = []
    for grade, label in GRADES:
        best = cheapest_open(grade)
        if not best:
            continue
        cards.append(
            f'<div class="card"><div class="card-grade">{label}</div>'
            f'<div class="card-price">${best["price"]:.2f}</div>'
            f'<div class="card-where">{esc(best["name"])}<br>{esc(best["street"])}, {esc(best["town"])}</div></div>'
        )

    regular_rows = "\n".join(price_row(r, now, i + 1) for i, r in enumerate(regular[:15]))
    other_tables = []
    for grade, label in GRADES[1:]:
        top = by_grade[grade][:5]
        if not top:
            continue
        other_tables.append(
            f"<h3>Cheapest {label.lower()}</h3>"
            f'<table class="prices"><tbody>{"".join(price_row(r, now, i + 1) for i, r in enumerate(top))}</tbody></table>'
        )

    towns: dict[str, list[dict]] = {}
    for r in rows:
        if r["grade"] == "regular":
            towns.setdefault(r["town"], []).append(r)
    town_sections = []
    for town in sorted(towns, key=lambda t: (-len(towns[t]), t)):
        items = sorted(towns[town], key=lambda r: r["price"])
        town_sections.append(
            f"<h3>Gas prices in {esc(town)}</h3>"
            f'<table class="prices"><tbody>{"".join(price_row(r, now) for r in items)}</tbody></table>'
        )

    spread_line = (
        f"<p class=\"spread\">Regular gas around Princeton ranges by <strong>${spread:.2f} a gallon</strong> "
        f"today — about <strong>${spread * 12:.0f}</strong> on a 12-gallon fill-up.</p>"
        if spread and spread >= 0.05
        else ""
    )

    structured = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "name": f"Gas prices in Princeton, NJ today ({date_text})",
        "url": PAGE_URL,
        "description": description,
        "dateModified": now.isoformat(),
        "about": {"@type": "Place", "name": "Princeton, New Jersey"},
    }

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gas Prices in Princeton, NJ Today ({esc(date_text)}) – Cheapest Stations | GasScout</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{PAGE_URL}">
<meta property="og:title" content="Gas prices in Princeton, NJ today">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{PAGE_URL}">
<meta property="og:type" content="website">
<script type="application/ld+json">{json.dumps(structured)}</script>
<style>
  :root {{ --green: #0E6B3D; --green-text: #0E6B3D; --ink: #111827; --muted: #4B5563; --line: #E5E7EB;
           --bg: #FFFFFF; --card: #F3F8F5; --stale: #B45309; --members: #2563EB; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --green-text: #34D399; --ink: #F3F4F6; --muted: #9CA3AF; --line: #2D333B;
             --bg: #111418; --card: #1A2420; --stale: #FBBF24; --members: #60A5FA; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--ink);
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; line-height: 1.5; }}
  header {{ background: var(--green); color: #fff; padding: 14px 16px; }}
  header a {{ color: #fff; text-decoration: none; font-weight: 800; font-size: 20px; }}
  main {{ max-width: 760px; margin: 0 auto; padding: 20px 16px 48px; }}
  h1 {{ font-size: 30px; line-height: 1.15; margin: 8px 0 4px; letter-spacing: -0.02em; }}
  h2 {{ font-size: 22px; margin: 36px 0 8px; }}
  h3 {{ font-size: 17px; margin: 24px 0 6px; }}
  .updated {{ color: var(--muted); font-size: 14px; margin: 0 0 16px; }}
  .spread {{ font-size: 17px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin: 16px 0; }}
  .card {{ background: var(--card); border-radius: 14px; padding: 14px; }}
  .card-grade {{ font-size: 13px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }}
  .card-price {{ font-size: 30px; font-weight: 800; color: var(--green-text); }}
  .card-where {{ font-size: 13px; color: var(--muted); }}
  .cta {{ background: var(--green); color: #fff; border-radius: 16px; padding: 18px; margin: 24px 0; }}
  .cta p {{ margin: 0 0 10px; font-size: 17px; font-weight: 600; }}
  .cta a {{ display: inline-block; background: #fff; color: #0E6B3D; font-weight: 800; text-decoration: none;
            padding: 10px 14px; border-radius: 10px; margin: 4px 8px 4px 0; }}
  table.prices {{ width: 100%; border-collapse: collapse; }}
  table.prices td {{ border-top: 1px solid var(--line); padding: 10px 6px; vertical-align: top; }}
  td.rank {{ color: var(--muted); width: 28px; }}
  td.price {{ text-align: right; font-weight: 800; font-size: 18px; color: var(--green-text); white-space: nowrap; }}
  .station {{ font-weight: 700; }}
  .where, .age {{ font-size: 13px; color: var(--muted); }}
  .age.stale {{ color: var(--stale); }}
  .badge.members {{ font-size: 11px; font-weight: 700; color: #fff; background: var(--members);
                    border-radius: 999px; padding: 1px 8px; vertical-align: middle; }}
  .note {{ font-size: 13px; color: var(--muted); margin-top: 32px; }}
  footer {{ text-align: center; font-size: 13px; color: var(--muted); padding: 24px 16px; }}
  footer a, .note a {{ color: var(--green-text); }}
</style>
</head>
<body>
<header><a href="{SITE_URL}">GasScout</a></header>
<main>
  <h1>Gas prices in Princeton, NJ today</h1>
  <p class="updated">Updated {esc(updated_text)} · {priced_count} stations within 10 miles of Princeton</p>
  {spread_line}

  <div class="cards">{"".join(cards)}</div>

  <div class="cta">
    <p>See every price on a map, get told when gas drops, and share prices from the pump. Free, no ads, no account needed.</p>
    <a href="{APP_STORE}">Download on the App Store</a>
    <a href="{PLAY_STORE}">Get it on Google Play</a>
  </div>

  <h2>Cheapest regular gas near Princeton</h2>
  <table class="prices"><tbody>
{regular_rows}
  </tbody></table>

  <h2>Other grades</h2>
  {"".join(other_tables)}

  <h2>Gas prices by town</h2>
  <p class="updated">Regular, cheapest first, for every station GasScout covers.</p>
  {"".join(town_sections)}

  <p class="note">Prices come from a daily fuel price feed and from drivers reporting what they see at the pump.
  They can lag behind the sign, so treat them as a guide. Warehouse clubs are marked "Members only" and left out of
  the cheapest headline. Spot a wrong price? Report it in the <a href="{APP_STORE}">GasScout app</a>.</p>
</main>
<footer>
  <a href="{SITE_URL}">GasScout</a> · <a href="{SITE_URL}privacy.html">Privacy</a> · <a href="{SITE_URL}support.html">Support</a><br>
  Made in Princeton, NJ.
</footer>
</body>
</html>
"""


def sitemap(now: datetime) -> str:
    day = now.date().isoformat()
    urls = [
        (SITE_URL, "weekly"),
        (PAGE_URL, "daily"),
        (SITE_URL + "support.html", "monthly"),
        (SITE_URL + "privacy.html", "monthly"),
    ]
    entries = "\n".join(
        f"  <url><loc>{u}</loc><lastmod>{day}</lastmod><changefreq>{f}</changefreq></url>" for u, f in urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}\n</urlset>\n"
    )


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    now = datetime.now(timezone.utc)
    stations = fetch_stations()
    if not stations:
        print("No stations returned; leaving the page as it is.", file=sys.stderr)
        return 1
    page = root / "gas-prices" / "index.html"
    page.parent.mkdir(exist_ok=True)
    page.write_text(build(stations, now), encoding="utf-8")
    (root / "sitemap.xml").write_text(sitemap(now), encoding="utf-8")
    print(f"Wrote {page.relative_to(root)} from {len(stations)} stations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
