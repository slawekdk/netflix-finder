import os
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
NETFLIX_TOP10_BASE = "https://www.netflix.com/tudum/top10/api/data"

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

def provider_ids(value):
    return None if value is None else str(value)

def normalize_title(value):
    value = str(value or "").lower()
    value = re.sub(r"\s*:\s*(season|part)\s+\d+.*$", "", value)
    value = re.sub(r"\s+(season|part)\s+\d+.*$", "", value)
    value = re.sub(r"[^a-z0-9ąćęłńóśźżäöüß]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def previous_week_date():
    # Netflix Top 10 weekly data is keyed by the Friday ending the
    # published Monday-Sunday reporting week (e.g. 2026-09-11 for 9/7-9/13).
    today = date.today()
    days_since_friday = (today.weekday() - 4) % 7
    this_friday = today - timedelta(days=days_since_friday)
    # If today is Friday, use the previous completed Friday.
    if today.weekday() == 4:
        this_friday -= timedelta(days=7)
    return this_friday.isoformat()

def candidate_week_dates():
    first = date.fromisoformat(previous_week_date())
    return [(first - timedelta(days=7*i)).isoformat() for i in range(6)]

def _walk_rows(obj):
    """Find dicts that look like Netflix Top 10 rows in a nested response."""
    rows = []
    if isinstance(obj, dict):
        keys = {str(k).lower() for k in obj.keys()}
        if ("views" in keys or "hoursviewed" in keys or "hours_viewed" in keys) and (
            "title" in keys or "name" in keys
        ):
            rows.append(obj)
        for value in obj.values():
            rows.extend(_walk_rows(value))
    elif isinstance(obj, list):
        for value in obj:
            rows.extend(_walk_rows(value))
    return rows

def _first(row, *keys):
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
        for actual in row:
            if str(actual).lower() == key.lower() and row[actual] not in (None, ""):
                return row[actual]
    return None

def _metric_number(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    s = str(value).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None

def _format_views(value):
    n = _metric_number(value)
    if n is None:
        return None
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return f"{int(n):,}"

async def netflix_top10(region: str, content_type: str, title: str, original_title: str = ""):
    """
    Read Netflix's public Top 10 data endpoint on a best-effort basis.
    Netflix has changed the public endpoint/schema over time, so try the
    most recent completed weekly keys and a few known category variants.
    """
    categories = ["films", "tv"] if content_type != "movie" else ["films"]
    titles = [title, original_title]
    target_titles = {normalize_title(x) for x in titles if x}

    def extract_rows(obj):
        rows = []
        if isinstance(obj, dict):
            keys = {str(k).lower().replace("-", "").replace("_", "") for k in obj.keys()}
            has_title = any(k in keys for k in ("title", "name"))
            has_metric = any(k in keys for k in (
                "views", "viewcount", "hoursviewed", "hours", "hourswatched"
            ))
            if has_title and has_metric:
                rows.append(obj)
            for value in obj.values():
                rows.extend(extract_rows(value))
        elif isinstance(obj, list):
            for value in obj:
                rows.extend(extract_rows(value))
        return rows

    def get_value(row, *wanted):
        wanted_norm = {w.lower().replace("-", "").replace("_", "") for w in wanted}
        for k, v in row.items():
            if str(k).lower().replace("-", "").replace("_", "") in wanted_norm and v not in (None, ""):
                return v
        return None

    for week in candidate_week_dates():
        for category in categories:
            params = {
                "category": category,
                "region": region.upper(),
                "week": week,
            }
            headers = {
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
                "Referer": "https://www.netflix.com/tudum/top10/",
            }
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    r = await client.get(NETFLIX_TOP10_BASE, params=params, headers=headers)
                    if r.status_code >= 400:
                        continue
                    data = r.json()
            except Exception:
                continue

            candidates = []
            for row in extract_rows(data):
                name = get_value(row, "title", "name")
                if not name:
                    continue
                normalized = normalize_title(name)
                score = 0
                for target in target_titles:
                    if normalized == target:
                        score = max(score, 100)
                    elif normalized.startswith(target) or target.startswith(normalized):
                        score = max(score, 80)
                    elif target in normalized or normalized in target:
                        score = max(score, 60)
                if score:
                    candidates.append((score, row))

            if not candidates:
                continue

            row = sorted(candidates, key=lambda x: x[0], reverse=True)[0][1]
            rank = get_value(row, "rank", "ranking", "position")
            views = get_value(row, "views", "viewCount", "view_count")
            hours = get_value(row, "hoursViewed", "hours_viewed", "hoursWatched", "hours")
            runtime = get_value(row, "runtime", "runtimeMinutes", "runtime_minutes")
            weeks = get_value(row, "weeksInTop10", "weeks_in_top10", "weeks")

            return {
                "rank": int(rank) if str(rank or "").isdigit() else rank,
                "views": _format_views(views),
                "hours_viewed": _format_views(hours),
                "runtime": runtime,
                "weeks_in_top10": int(weeks) if str(weeks or "").isdigit() else weeks,
                "week": week,
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
