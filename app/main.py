import os
import re
import asyncio
import time
import html as html_lib
from datetime import date, timedelta
from typing import Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles

TMDB_TOKEN = os.getenv("TMDB_TOKEN")
TMDB_BASE = "https://api.themoviedb.org/3"
NETFLIX_PROVIDER_ID = 8
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "DK")

NETFLIX_API = "https://www.netflix.com/tudum/top10/api/data"
NETFLIX_CACHE = {}
NETFLIX_CACHE_TTL = 3600

app = FastAPI(
    title="Netflix Finder",
    version="1.6.0",
    description="Find Netflix titles by production country, type, rating, vote count, popularity and Netflix Top 10 metrics.",
)

class UTF8JSONMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("application/json"):
            response.headers["content-type"] = "application/json; charset=utf-8"
        return response

app.add_middleware(UTF8JSONMiddleware)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", include_in_schema=False)
async def frontend():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

def provider_ids(value):
    return None if value is None else str(value)

async def tmdb_get(path: str, params: dict):
    if not TMDB_TOKEN:
        raise HTTPException(500, "TMDB_TOKEN is not configured.")
    headers = {"Authorization": f"Bearer {TMDB_TOKEN}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{TMDB_BASE}{path}", params=params, headers=headers)
        if response.is_error:
            raise HTTPException(response.status_code, f"TMDB request failed: HTTP {response.status_code}")
        return response.json()

def normalize_title(value):
    value = str(value or "").lower()
    value = re.sub(r"\s*:\s*(season|part)\s+\d+.*$", "", value)
    value = re.sub(r"\s+(season|part)\s+\d+.*$", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


# Netflix's old /api/data endpoint now returns the Tudum HTML page.
# We therefore parse the public Tudum Top 10 pages instead.
NETFLIX_PAGES = {
    "movie_global": "https://www.netflix.com/tudum/top10/most-pop",
    "tv_global": "https://www.netflix.com/tudum/top10/tv",
    "movie_dk": "https://www.netflix.com/tudum/top10/denmark",
    "tv_dk": "https://www.netflix.com/tudum/top10/denmark/tv/2021-08-15",
}

NETFLIX_PAGE_CACHE = {}
NETFLIX_PAGE_CACHE_TTL = 3600


def netflix_week_label(html):
    # e.g. "Global | 9/7/26 - 9/13/26" or "Denmark | 9/7/26 - 9/13/26"
    m = re.search(r"(?:Global|Denmark)\s*\|\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*-\s*(\d{1,2}/\d{1,2}/\d{2,4})", html)
    return f"{m.group(1)} - {m.group(2)}" if m else None


def netflix_extract_rows(html, include_metrics=True):
    """
    Extract the Top 10 overview table from Tudum's server-rendered HTML.
    We intentionally parse the text around the 'Top 10 ... Overview' table
    rather than relying on Netflix's retired JSON endpoint.
    """
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    # Normalize common separators that appear in rendered table text.
    text = text.replace("Ranking Views Runtime Hours Viewed", "Ranking Views Runtime Hours Viewed")
    rows = []

    # The page contains the title, then its weekly views, then the rank.
    # The overview table later repeats the title followed by numeric columns.
    # We first locate the overview section and parse its 10 numbered entries.
    overview_match = re.search(
        r"Top 10 (?:Movies|Shows) Overview\s+(.*?)(?:Explore The Most Watched|Understand the Methodology)",
        text,
        flags=re.I,
    )
    section = overview_match.group(1) if overview_match else text

    # Titles in the overview are separated by the numeric rank. Because
    # titles can contain punctuation and colons, capture up to the next
    # 1-10 rank marker.
    pattern = re.compile(
        r"(?:^|\s)(0?[1-9]|10)\s+(.+?)\s+(?:\1\s+)?(\d{1,3}(?:,\d{3})+|\d+)(?:\s+(\d+:\d{2})\s+(\d{1,3}(?:,\d{3})+))?",
        re.I,
    )

    for m in pattern.finditer(section):
        rank = int(m.group(1))
        title = m.group(2).strip()
        # Avoid accidentally capturing the column heading.
        if title.lower() in {"image", "button"}:
            continue

        # For global pages the table has:
        # rank | title | secondary rank | views | runtime | hours
        # The regex above can be ambiguous, so also inspect the nearby
        # substring and use the last numeric fields where available.
        views = None
        runtime = None
        hours = None

        tail = section[m.start():m.end() + 100]
        nums = re.findall(r"\b\d{1,3}(?:,\d{3})+\b", tail)
        runtimes = re.findall(r"\b\d+:\d{2}\b", tail)

        if include_metrics and len(nums) >= 1:
            views = nums[-2] if len(nums) >= 2 else nums[-1]
            hours = nums[-1] if len(nums) >= 2 else None
        if runtimes:
            runtime = runtimes[0]

        rows.append({
            "rank": rank,
            "title": title,
            "views": views,
            "runtime": runtime,
            "hours_viewed": hours,
        })

    # Fallback parser: use the visible cards if the table parser did not
    # find rows. This still gives us a usable title/rank match.
    if not rows:
        card_match = re.search(
            r"Top 10 (?:Movies|Shows).*?(?:Overview)",
            text,
            flags=re.I,
        )
        if card_match:
            prefix = text[card_match.start():]
            for m in re.finditer(
                r"(?:#(10|[1-9]))\s+(.{2,100}?)(?=\s+\d+(?:\.\d+)?M views this week)",
                prefix,
                flags=re.I,
            ):
                rows.append({
                    "rank": int(m.group(1)),
                    "title": m.group(2).strip(),
                    "views": None,
                    "runtime": None,
                    "hours_viewed": None,
                })

    return rows


async def fetch_netflix_page(key):
    cached = NETFLIX_PAGE_CACHE.get(key)
    if cached and time.time() - cached[0] < NETFLIX_PAGE_CACHE_TTL:
        return cached[1]

    url = NETFLIX_PAGES[key]
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            if response.is_error:
                return None
            html = response.text
    except Exception:
        return None

    result = {
        "html": html,
        "week": netflix_week_label(html),
        "rows": netflix_extract_rows(html, include_metrics=("global" in key)),
    }

    NETFLIX_PAGE_CACHE[key] = (time.time(), result)
    return result


async def netflix_top10(region: str, content_type: str, title: str, original_title: str = ""):
    targets = {normalize_title(x) for x in (title, original_title) if x}
    if not targets:
        return None

    is_movie = content_type == "movie"
    global_key = "movie_global" if is_movie else "tv_global"
    dk_key = "movie_dk" if is_movie else "tv_dk"

    # Fetch global metrics and DK ranking concurrently. Each page is cached
    # for an hour, so opening several titles does not repeatedly hit Netflix.
    global_data, dk_data = await asyncio.gather(
        fetch_netflix_page(global_key),
        fetch_netflix_page(dk_key) if region.upper() == "DK" else asyncio.sleep(0, result=None),
    )

    match_global = None
    if global_data:
        for row in global_data["rows"]:
            name = normalize_title(row.get("title"))
            if name in targets or any(
                name.startswith(t) or t.startswith(name) for t in targets
            ):
                match_global = row
                break

    match_dk = None
    if dk_data:
        for row in dk_data["rows"]:
            name = normalize_title(row.get("title"))
            if name in targets or any(
                name.startswith(t) or t.startswith(name) for t in targets
            ):
                match_dk = row
                break

    if not match_global and not match_dk:
        return None

    return {
        "rank": match_dk.get("rank") if match_dk else None,
        "global_rank": match_global.get("rank") if match_global else None,
        "views": match_global.get("views") if match_global else None,
        "hours_viewed": match_global.get("hours_viewed") if match_global else None,
        "week": (
            global_data.get("week") if global_data
            else (dk_data.get("week") if dk_data else None)
        ),
        "source": "Netflix Tudum Top 10",
        "rank_region": region.upper() if match_dk else None,
    }

@app.get("/health")
async def health():
    return {"ok": True, "tmdb_configured": bool(TMDB_TOKEN)}

@app.get("/search")
async def search(
    production_country: Optional[str] = Query(None),
    content_type: Literal["movie", "series", "miniseries", "all"] = "all",
    min_rating: float = Query(0, ge=0, le=10),
    min_votes: int = Query(0, ge=0),
    sort: Literal["rating", "votes", "popularity"] = "rating",
    region: str = Query(DEFAULT_REGION, min_length=2, max_length=2),
    page: int = Query(1, ge=1, le=20),
    limit: int = Query(10, ge=1, le=20),
):
    sort_map = {
        "rating": "vote_average.desc",
        "votes": "vote_count.desc",
        "popularity": "popularity.desc",
    }
    common = {
        "language": "en-US",
        "watch_region": region.upper(),
        "with_watch_providers": provider_ids(NETFLIX_PROVIDER_ID),
        "with_watch_monetization_types": "flatrate",
        "vote_average.gte": min_rating,
        "vote_count.gte": min_votes,
        "page": page,
        "sort_by": sort_map[sort],
        "include_adult": "false",
    }
    if production_country:
        common["with_origin_country"] = production_country.upper()

    if content_type == "movie":
        data = await tmdb_get("/discover/movie", common)
        items = [{"type": "movie", **x} for x in data.get("results", [])]
    elif content_type in ("series", "miniseries"):
        data = await tmdb_get("/discover/tv", common)
        items = [{"type": "series", **x} for x in data.get("results", [])]
    else:
        movie_data, tv_data = await asyncio.gather(
            tmdb_get("/discover/movie", common),
            tmdb_get("/discover/tv", common),
        )
        items = [{"type": "movie", **x} for x in movie_data.get("results", [])]
        items += [{"type": "series", **x} for x in tv_data.get("results", [])]

    out = []
    for item in items:
        media_type = item["type"]
        title = item.get("title") if media_type == "movie" else item.get("name")
        date_value = item.get("release_date") if media_type == "movie" else item.get("first_air_date")
        out.append({
            "id": item.get("id"),
            "type": media_type,
            "title": title,
            "original_title": item.get("original_title") or item.get("original_name"),
            "year": (date_value or "")[:4] or None,
            "rating": item.get("vote_average"),
            "vote_count": item.get("vote_count"),
            "popularity": item.get("popularity"),
            "overview": item.get("overview"),
            "poster_path": item.get("poster_path"),
            "tmdb_url": f"https://www.themoviedb.org/{'movie' if media_type == 'movie' else 'tv'}/{item.get('id')}",
        })

    if content_type == "miniseries":
        filtered = []
        for item in out:
            details = await tmdb_get(f"/tv/{item['id']}", {"language": "en-US"})
            if str(details.get("type", "")).lower() == "miniseries":
                item["type"] = "miniseries"
                filtered.append(item)
        out = filtered

    sort_key = {"rating": "rating", "votes": "vote_count", "popularity": "popularity"}[sort]
    out.sort(key=lambda item: item.get(sort_key) or 0, reverse=True)

    return JSONResponse(content={
        "region": region.upper(),
        "provider": "Netflix",
        "criteria": {
            "production_country": production_country.upper() if production_country else None,
            "content_type": content_type,
            "min_rating": min_rating,
            "min_votes": min_votes,
            "sort": sort,
        },
        "results": out[:limit],
        "data_notes": [
            "Netflix availability is sourced through TMDB watch-provider data, powered by JustWatch.",
            "Netflix Top 10 metrics are read from Netflix's public Top 10 data endpoint when available.",
        ],
    })

@app.get("/title/{media_type}/{tmdb_id}")
async def get_title(media_type: Literal["movie", "tv"], tmdb_id: int):
    details = await tmdb_get(
        f"/{media_type}/{tmdb_id}",
        {"language": "en-US", "append_to_response": "watch/providers"},
    )
    title = details.get("title") or details.get("name")
    date_value = details.get("release_date") or details.get("first_air_date")
    countries = [
        c.get("iso_3166_1")
        for c in details.get("production_countries", [])
        if c.get("iso_3166_1")
    ]
    region = DEFAULT_REGION.upper()
    providers = details.get("watch/providers", {}).get("results", {}).get(region, {})
    netflix = [
        p for p in providers.get("flatrate", [])
        if p.get("provider_id") == NETFLIX_PROVIDER_ID
    ]

    netflix_data = await netflix_top10(
        region,
        "movie" if media_type == "movie" else "series",
        title,
        details.get("original_title") or details.get("original_name") or "",
    )

    return {
        "id": details.get("id"),
        "type": "movie" if media_type == "movie" else (
            "miniseries" if str(details.get("type", "")).lower() == "miniseries" else "series"
        ),
        "title": title,
        "original_title": details.get("original_title") or details.get("original_name"),
        "year": (date_value or "")[:4] or None,
        "production_countries": countries,
        "rating": details.get("vote_average"),
        "vote_count": details.get("vote_count"),
        "popularity": details.get("popularity"),
        "runtime_minutes": details.get("runtime") or (details.get("episode_run_time") or [None])[0],
        "overview": details.get("overview"),
        "poster_url": (
            f"https://image.tmdb.org/t/p/w500{details['poster_path']}"
            if details.get("poster_path") else None
        ),
        "netflix_available_in_default_region": bool(netflix),
        "netflix_views": netflix_data.get("views") if netflix_data else None,
        "hours_viewed": netflix_data.get("hours_viewed") if netflix_data else None,
        "netflix_rank": netflix_data.get("rank") if netflix_data else None,
        "netflix_weeks_in_top10": netflix_data.get("weeks_in_top10") if netflix_data else None,
        "netflix_metrics_week": netflix_data.get("week") if netflix_data else None,
        "netflix_metrics_source": netflix_data.get("source") if netflix_data else None,
        "tmdb_url": f"https://www.themoviedb.org/{media_type}/{tmdb_id}",
    }
