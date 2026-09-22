import os
import asyncio
import html
import time
import re
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
NETFLIX_TOP10_BASE = "https://www.netflix.com/tudum/top10"
NETFLIX_CACHE = {}
NETFLIX_CACHE_TTL = 3600

app = FastAPI(
    title="Netflix Finder",
    version="1.4.0",
    description="Find Netflix titles by production country, type, rating, vote count, popularity and Netflix Top 10 metrics."
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

async def tmdb_get(path: str, params: dict):
    if not TMDB_TOKEN:
        raise HTTPException(500, "TMDB_TOKEN is not configured.")
    headers = {"Authorization": f"Bearer {TMDB_TOKEN}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{TMDB_BASE}{path}", params=params, headers=headers)
        if response.is_error:
            raise HTTPException(response.status_code, f"TMDB request failed: HTTP {response.status_code}")
        return response.json()

d
def normalize_title(value):
    value = str(value or "").lower()
    value = re.sub(r"\\s*:\\s*(season|part)\\s+\\d+.*$", "", value)
    value = re.sub(r"\\s+(season|part)\\s+\\d+.*$", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\\s+", " ", value).strip()

def previous_completed_week():
    # Netflix weekly charts are Monday-Sunday; the week parameter is Sunday.
    today = date.today()
    sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    if sunday >= today:
        sunday -= timedelta(days=7)
    return sunday.isoformat()

def clean_html(raw):
    raw = re.sub(r"<script\\b[^>]*>.*?</script>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<style\\b[^>]*>.*?</style>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    return re.sub(r"\\s+", " ", raw).strip()

def title_context(text, titles):
    if not text:
        return None
    for title in titles:
        if title:
            m = re.search(re.escape(title), text, flags=re.I)
            if m:
                return text[max(0, m.start()-300):m.start()+1800]
    return None

def parse_rank(context):
    if not context:
        return None
    m = re.search(r"#\\s*(\\d{1,2})\\s+in\\s+(?:Movies|Shows)", context, re.I)
    return int(m.group(1)) if m else None

def parse_views(context):
    if not context:
        return None
    m = re.search(r"([\\d,.]+)\\s*([MK]?)\\s+views\\s+this\\s+week", context, re.I)
    return f"{m.group(1)}{m.group(2).upper()}" if m else None

def parse_hours(context):
    if not context:
        return None
    m = re.search(r"([\\d,.]+[MK]?)\\s+Hours\\s+Viewed", context, re.I)
    return m.group(1) if m else None

async def netflix_top10(region, content_type, title, original_title=""):
    # Netflix country charts provide the DK rank. Global charts provide
    # weekly Views and Hours Viewed. Fetch both pages concurrently.
    if region.upper() != "DK":
        return None

    week = previous_completed_week()
    kind = "tv" if content_type in ("series", "miniseries") else "movie"

    country_path = "denmark/tv" if kind == "tv" else "denmark"
    global_path = "tv" if kind == "tv" else "most-pop"

    urls = [
        f"https://www.netflix.com/tudum/top10/{country_path}?week={week}",
        f"https://www.netflix.com/tudum/top10/{global_path}?week={week}",
    ]

    cache_key = (kind, normalize_title(title), week)
    cached = NETFLIX_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < NETFLIX_CACHE_TTL:
        return cached[1]

    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": "Mozilla/5.0",
    }

    async def fetch(url):
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
                r = await client.get(url, headers=headers)
                return clean_html(r.text) if not r.is_error else ""
        except Exception:
            return ""

    country_text, global_text = await asyncio.gather(fetch(urls[0]), fetch(urls[1]))

    candidates = [title, original_title]
    country_ctx = title_context(country_text, candidates)
    global_ctx = title_context(global_text, candidates)

    result = {
        "rank": parse_rank(country_ctx),
        "views": parse_views(global_ctx),
        "hours_viewed": parse_hours(global_ctx),
        "week": week,
        "source": "Netflix Top 10",
    }

    if result["rank"] is None and result["views"] is None and result["hours_viewed"] is None:
        result = None

    NETFLIX_CACHE[cache_key] = (time.time(), result)
    return result
               "source": "Netflix Top 10",
            }

    return None

@app.get("/health")
async def health():
    return {"ok": True, "tmdb_configured": bool(TMDB_TOKEN)}

@app.get("/search")
async def search(
    production_country: Optional[str] = Query(None, description="ISO 3166-1 country code, e.g. KR, DK, US"),
    content_type: Literal["movie", "series", "miniseries", "all"] = "all",
    min_rating: float = Query(0, ge=0, le=10),
    min_votes: int = Query(0, ge=0),
    sort: Literal["rating", "votes", "popularity"] = "rating",
    region: str = Query(DEFAULT_REGION, min_length=2, max_length=2),
    page: int = Query(1, ge=1, le=20),
    limit: int = Query(10, ge=1, le=20),
):
    sort_map = {"rating": "vote_average.desc", "votes": "vote_count.desc", "popularity": "popularity.desc"}
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
        movie_data = await tmdb_get("/discover/movie", common)
        tv_data = await tmdb_get("/discover/tv", common)
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
            "netflix_views": None,
            "hours_viewed": None,
            "netflix_rank": None,
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
    countries = [c.get("iso_3166_1") for c in details.get("production_countries", []) if c.get("iso_3166_1")]
    region = DEFAULT_REGION.upper()
    providers = details.get("watch/providers", {}).get("results", {}).get(region, {})
    netflix = [p for p in providers.get("flatrate", []) if p.get("provider_id") == NETFLIX_PROVIDER_ID]

    netflix_data = await netflix_top10(
        region,
        "movie" if media_type == "movie" else "series",
        title,
        details.get("original_title") or details.get("original_name") or "",
    )

    return {
        "id": details.get("id"),
        "type": "movie" if media_type == "movie" else ("miniseries" if str(details.get("type", "")).lower() == "miniseries" else "series"),
        "title": title,
        "original_title": details.get("original_title") or details.get("original_name"),
        "year": (date_value or "")[:4] or None,
        "production_countries": countries,
        "rating": details.get("vote_average"),
        "vote_count": details.get("vote_count"),
        "popularity": details.get("popularity"),
        "runtime_minutes": details.get("runtime") or (details.get("episode_run_time") or [None])[0],
        "overview": details.get("overview"),
        "poster_url": f"https://image.tmdb.org/t/p/w500{details['poster_path']}" if details.get("poster_path") else None,
        "netflix_available_in_default_region": bool(netflix),
        "netflix_views": netflix_data.get("views") if netflix_data else None,
        "hours_viewed": netflix_data.get("hours_viewed") if netflix_data else None,
        "netflix_rank": netflix_data.get("rank") if netflix_data else None,
        "netflix_weeks_in_top10": netflix_data.get("weeks_in_top10") if netflix_data else None,
        "netflix_metrics_week": netflix_data.get("week") if netflix_data else None,
        "netflix_metrics_source": netflix_data.get("source") if netflix_data else None,
        "tmdb_url": f"https://www.themoviedb.org/{media_type}/{tmdb_id}",
    }
