import os
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

app = FastAPI(
    title="Netflix Finder",
    version="1.2.0",
    description="Find Netflix titles by production country, type, rating, vote count and popularity."
)

class UTF8JSONMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("application/json"):
            response.headers["content-type"] = "application/json; charset=utf-8"
        return response

app.add_middleware(UTF8JSONMiddleware)

# Mobile-first web UI
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
        date = item.get("release_date") if media_type == "movie" else item.get("first_air_date")
        out.append({
            "id": item.get("id"),
            "type": media_type,
            "title": title,
            "original_title": item.get("original_title") or item.get("original_name"),
            "year": (date or "")[:4] or None,
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
            "Netflix viewing metrics are reserved for a later official Netflix Top 10 integration.",
        ],
    })

@app.get("/title/{media_type}/{tmdb_id}")
async def get_title(media_type: Literal["movie", "tv"], tmdb_id: int):
    details = await tmdb_get(
        f"/{media_type}/{tmdb_id}",
        {"language": "en-US", "append_to_response": "watch/providers"},
    )
    title = details.get("title") or details.get("name")
    date = details.get("release_date") or details.get("first_air_date")
    countries = [c.get("iso_3166_1") for c in details.get("production_countries", []) if c.get("iso_3166_1")]
    region = DEFAULT_REGION.upper()
    providers = details.get("watch/providers", {}).get("results", {}).get(region, {})
    netflix = [p for p in providers.get("flatrate", []) if p.get("provider_id") == NETFLIX_PROVIDER_ID]
    return {
        "id": details.get("id"),
        "type": "movie" if media_type == "movie" else ("miniseries" if str(details.get("type", "")).lower() == "miniseries" else "series"),
        "title": title,
        "original_title": details.get("original_title") or details.get("original_name"),
        "year": (date or "")[:4] or None,
        "production_countries": countries,
        "rating": details.get("vote_average"),
        "vote_count": details.get("vote_count"),
        "popularity": details.get("popularity"),
        "runtime_minutes": details.get("runtime") or (details.get("episode_run_time") or [None])[0],
        "overview": details.get("overview"),
        "poster_url": f"https://image.tmdb.org/t/p/w500{details['poster_path']}" if details.get("poster_path") else None,
        "netflix_available_in_default_region": bool(netflix),
        "netflix_views": None,
        "hours_viewed": None,
        "netflix_rank": None,
        "tmdb_url": f"https://www.themoviedb.org/{media_type}/{tmdb_id}",
    }
