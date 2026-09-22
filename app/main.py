import os
from typing import Literal, Optional
import httpx
from fastapi import FastAPI, HTTPException, Query

TMDB_TOKEN = os.getenv("TMDB_TOKEN")
TMDB_BASE = "https://api.themoviedb.org/3"
NETFLIX_PROVIDER_ID = 8  # Netflix in TMDB
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "DK")

app = FastAPI(
    title="Netflix Finder",
    version="1.0.0",
    description="Find Netflix titles by production country, type, rating, vote count and Netflix viewing data."
)

async def tmdb_get(path: str, params: dict):
    if not TMDB_TOKEN:
        raise HTTPException(500, "TMDB_TOKEN is not configured.")
    headers = {"Authorization": f"Bearer {TMDB_TOKEN}", "accept": "application/json"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{TMDB_BASE}{path}", params=params, headers=headers)
        r.raise_for_status()
        return r.json()

def provider_ids(value):
    if value is None:
        return None
    return str(value)

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
    """Search TMDB titles that are currently listed as Netflix streaming in the selected region."""
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
        items = [{"type":"movie", **x} for x in data.get("results", [])]
    elif content_type in ("series", "miniseries"):
        data = await tmdb_get("/discover/tv", common)
        items = []
        for x in data.get("results", []):
            # TMDB marks limited series with type=Miniseries on detail records.
            # We filter below after fetching details.
            items.append({"type":"series", **x})
    else:
        m = await tmdb_get("/discover/movie", common)
        t = await tmdb_get("/discover/tv", common)
        items = [{"type":"movie", **x} for x in m.get("results", [])]
        items += [{"type":"series", **x} for x in t.get("results", [])]

    # Normalize titles and optionally detect miniseries.
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
            "tmdb_url": f"https://www.themoviedb.org/{'movie' if media_type=='movie' else 'tv'}/{item.get('id')}",
        })

    if content_type == "miniseries":
        filtered = []
        for x in out[:limit]:
            details = await tmdb_get(f"/tv/{x['id']}", {"language":"en-US"})
            if str(details.get("type","")).lower() == "miniseries":
                x["type"] = "miniseries"
                filtered.append(x)
        out = filtered
    else:
        out = out[:limit]

    return {
        "region": region.upper(),
        "provider": "Netflix",
        "criteria": {
            "production_country": production_country.upper() if production_country else None,
            "content_type": content_type,
            "min_rating": min_rating,
            "min_votes": min_votes,
            "sort": sort,
        },
        "results": out,
        "note": "Netflix availability is sourced through TMDB's watch-provider data, powered by JustWatch."
    }
