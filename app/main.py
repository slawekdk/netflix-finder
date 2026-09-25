import os
import re
import time
from typing import Optional

import httpx
from html.parser import HTMLParser
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Netflix Finder by Slawek", version="1.8.0")

TMDB_TOKEN = os.getenv("TMDB_TOKEN", "")
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "DK")
TOP10_CACHE_TTL = 3600
_top10_cache = {}

def tmdb_headers():
    if not TMDB_TOKEN:
        raise HTTPException(status_code=500, detail="TMDB_TOKEN is not configured")
    return {"Authorization": f"Bearer {TMDB_TOKEN}", "Content-Type": "application/json"}

def tmdb_get(path: str, params=None):
    r = httpx.get(f"https://api.themoviedb.org/3{path}", headers=tmdb_headers(),
                  params=params or {}, timeout=20, follow_redirects=True)
    r.raise_for_status()
    return r.json()

def normalize_title(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip().lower())
    value = re.sub(r"[:\-–—'’.,!?()]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def netflix_top10_url(region: str, media_type: str) -> str:
    region = (region or DEFAULT_REGION).upper()
    if region == "DK":
        if media_type == "tv":
            return "https://www.netflix.com/tudum/top10/denmark/tv/2021-08-15"
        return "https://www.netflix.com/tudum/top10/denmark"
    if media_type == "tv":
        return "https://www.netflix.com/tudum/top10/tv"
    return "https://www.netflix.com/tudum/top10/most-pop"

def parse_number(value: str) -> Optional[float]:
    if not value:
        return None
    s = value.strip().upper().replace(",", "")
    m = re.search(r"([\d.]+)\s*([KMB])?", s)
    if not m:
        return None
    n = float(m.group(1))
    if m.group(2) == "K": n *= 1_000
    elif m.group(2) == "M": n *= 1_000_000
    elif m.group(2) == "B": n *= 1_000_000_000
    return n

class _Top10HTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_tr = False
        self.in_cell = False
        self.current_cell = []
        self.current_row = []
        self.rows = []
        self.text_parts = []
        self.last_image_alt = None
        self.image_rank_items = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        if tag == "img":
            alt = (attrs.get("alt") or "").strip()
            if alt and alt.lower() not in {"image", "poster", "thumbnail"}:
                self.last_image_alt = alt
        if tag == "tr":
            self.in_tr = True
            self.current_row = []
        elif self.in_tr and tag in ("td", "th"):
            self.in_cell = True
            self.current_cell = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.in_cell:
            self.current_row.append(re.sub(r"\s+", " ", " ".join(self.current_cell)).strip())
            self.in_cell = False
        elif tag == "tr" and self.in_tr:
            if self.current_row:
                self.rows.append(self.current_row)
            self.in_tr = False
        elif tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "section", "br"):
            self.text_parts.append("\n")

    def handle_data(self, data):
        value = data.strip()
        if self.in_cell:
            self.current_cell.append(value)
        self.text_parts.append(value)
        m = re.search(r"#\s*(10|[1-9])\s+in\s+(Movies|Shows)\b", value, re.I)
        if m and self.last_image_alt:
            self.image_rank_items.append({
                "title": re.sub(r"^Image:\s*", "", self.last_image_alt, flags=re.I).strip(),
                "rank": int(m.group(1)),
                "category": m.group(2).lower(),
            })

def parse_top10_html(html: str, region: str, media_type: str):
    parser = _Top10HTMLParser()
    parser.feed(html)
    text = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()
    week = None
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{2})\s*[–—-]\s*(\d{1,2}/\d{1,2}/\d{2})", text)
    if m:
        week = f"{m.group(1)} - {m.group(2)}"

    results = []
    for cells in parser.rows:
        if len(cells) < 2:
            continue
        title = None
        for cell in cells:
            cleaned = re.sub(r"^\d+\s*", "", cell).strip()
            if cleaned and not re.fullmatch(r"[\d.,:+\-–—]+", cleaned) and not re.fullmatch(
                r"(Ranking|Views|Runtime|Hours Viewed)", cleaned, re.I):
                title = cleaned
                break
        if not title:
            continue
        rank = None
        for cell in cells[:2]:
            mr = re.search(r"\b(10|[1-9])\b", cell)
            if mr:
                rank = int(mr.group(1))
                break
        views = hours = runtime = None
        for cell in cells:
            if re.search(r"\d[\d,.]*\s*(?:K|M|B)?\s*views?", cell, re.I):
                views = parse_number(cell)
            if re.search(r"\d[\d,.]*\s*(?:K|M|B)?\s*(?:hours?)", cell, re.I):
                hours = parse_number(cell)
            if re.fullmatch(r"\d{1,2}:\d{2}", cell):
                runtime = cell
        if rank is not None and 1 <= rank <= 10:
            results.append({"title": title, "rank": rank, "views": views,
                            "hours_viewed": hours, "runtime": runtime})

    wanted_category = "shows" if media_type == "tv" else "movies"
    if not results:
        direct_pattern = re.compile(
            r'<img[^>]+alt=["\'](?!Image["\'])([^"\']+)["\'][^>]*>'
            r'.{0,2500}?#\s*(10|[1-9])\s+in\s+(Movies|Shows)\b', re.I | re.S)
        for match in direct_pattern.finditer(html):
            if match.group(3).lower() == wanted_category:
                results.append({"title": re.sub(r"\s+", " ", match.group(1)).strip(),
                                "rank": int(match.group(2)), "views": None,
                                "hours_viewed": None, "runtime": None})

    if not results:
        for item in parser.image_rank_items:
            if item["category"] == wanted_category:
                results.append({"title": item["title"], "rank": item["rank"],
                                "views": None, "hours_viewed": None, "runtime": None})

    unique = {(x["rank"], normalize_title(x["title"])): x for x in results}
    return {"region": region.upper(), "type": media_type, "week": week,
            "results": sorted(unique.values(), key=lambda x: x["rank"])[:10],
            "source": netflix_top10_url(region, media_type)}

def get_top10(region: str, media_type: str):
    key = f"{region.upper()}:{media_type}"
    now = time.time()
    cached = _top10_cache.get(key)
    if cached and now - cached["timestamp"] < TOP10_CACHE_TTL:
        return cached["data"]
    r = httpx.get(netflix_top10_url(region, media_type), headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"}, timeout=25, follow_redirects=True)
    r.raise_for_status()
    data = parse_top10_html(r.text, region, media_type)
    _top10_cache[key] = {"timestamp": now, "data": data}
    return data

def match_top10(title: str, top10_data: dict):
    wanted = normalize_title(title)
    if not wanted:
        return None
    for item in top10_data.get("results", []):
        if normalize_title(item["title"]) == wanted:
            return item
    for item in top10_data.get("results", []):
        candidate = normalize_title(item["title"])
        if wanted in candidate or candidate in wanted:
            return item
    return None

def enrich_with_top10(item: dict, region: str):
    top10_type = "tv" if item.get("type") == "tv" else "movie"
    try:
        data = get_top10(region, top10_type)
        hit = match_top10(item.get("title") or item.get("name") or "", data)
        item["netflix_rank"] = hit["rank"] if hit else None
        item["netflix_views"] = hit["views"] if hit else None
        item["hours_viewed"] = hit["hours_viewed"] if hit else None
        item["netflix_runtime"] = hit["runtime"] if hit else None
        item["netflix_metrics_week"] = data.get("week")
        item["netflix_metrics_source"] = data.get("source")
    except Exception:
        item["netflix_rank"] = item["netflix_views"] = item["hours_viewed"] = None
        item["netflix_runtime"] = item["netflix_metrics_week"] = item["netflix_metrics_source"] = None
    return item

def production_countries(names):
    return [{"name": x.get("name")} for x in (names or []) if x.get("name")]

@app.get("/health")
def health():
    return {"ok": True, "tmdb_configured": bool(TMDB_TOKEN), "version": app.version}

@app.get("/search")
def search(
    query: str = Query(..., min_length=1),
    region: str = DEFAULT_REGION,
    media_type: str = "all",
    content_type: Optional[str] = None,
    min_rating: float = 0,
    min_votes: int = 0,
    country: str = "",
    production_country: Optional[str] = None,
    sort_by: str = "rating",
    sort: Optional[str] = None,
    limit: int = 20,
):
    region = region.upper()
    media_type = (content_type or media_type or "all").lower()
    country = (production_country or country or "").upper()
    sort_by = (sort or sort_by or "rating").lower()
    if media_type in ("series", "miniseries"):
        media_type = "tv"

    data = tmdb_get("/search/multi", {
        "query": query, "include_adult": "false", "language": "en-US", "page": 1})

    results = []
    for x in data.get("results", []):
        tmdb_type = x.get("media_type")
        if tmdb_type not in ("movie", "tv"):
            continue
        if media_type == "movie" and tmdb_type != "movie":
            continue
        if media_type == "tv" and tmdb_type != "tv":
            continue
        rating = float(x.get("vote_average") or 0)
        votes = int(x.get("vote_count") or 0)
        if rating < min_rating or votes < min_votes:
            continue
        title = x.get("title") if tmdb_type == "movie" else x.get("name")
        date = x.get("release_date") if tmdb_type == "movie" else x.get("first_air_date")
        results.append({
            "id": x["id"], "type": tmdb_type, "title": title,
            "original_title": x.get("original_title") or x.get("original_name"),
            "year": date[:4] if date else None, "rating": rating,
            "vote_count": votes, "popularity": float(x.get("popularity") or 0),
            "overview": x.get("overview") or "", "poster_path": x.get("poster_path")})

    filtered = []
    for item in results:
        try:
            providers = tmdb_get(f"/{item['type']}/{item['id']}/watch/providers").get("results", {})
            region_data = providers.get(region, {})
            netflix = any(p.get("provider_name", "").lower() == "netflix"
                          for p in (region_data.get("flatrate") or []))
            item["netflix_available"] = netflix
            if netflix:
                filtered.append(item)
        except Exception:
            continue

    if country:
        country_filtered = []
        for item in filtered:
            try:
                details = tmdb_get(f"/{item['type']}/{item['id']}")
                codes = {c.get("iso_3166_1", "").upper()
                         for c in details.get("production_countries", [])}
                if country in codes:
                    country_filtered.append(item)
            except Exception:
                pass
        filtered = country_filtered

    if sort_by == "votes":
        filtered.sort(key=lambda x: x["vote_count"], reverse=True)
    elif sort_by == "popularity":
        filtered.sort(key=lambda x: x["popularity"], reverse=True)
    else:
        filtered.sort(key=lambda x: (x["rating"], x["vote_count"]), reverse=True)

    return {
        "query": query, "region": region,
        "criteria": {"media_type": media_type, "country": country,
                     "min_rating": min_rating, "min_votes": min_votes,
                     "sort": sort_by, "limit": limit},
        "results": filtered[:max(1, min(limit, 50))]
    }

@app.get("/top10")
def top10_debug(region: str = DEFAULT_REGION, media_type: str = "movie"):
    media_type = "tv" if media_type.lower() in ("tv", "series", "shows") else "movie"
    return get_top10(region.upper(), media_type)

@app.get("/lookup")
def lookup(title: str = Query(..., min_length=1), region: str = DEFAULT_REGION):
    region = region.upper()
    found = []
    for media_type in ("movie", "tv"):
        data = tmdb_get(f"/search/{media_type}", {
            "query": title, "include_adult": "false", "language": "en-US", "page": 1})
        for x in data.get("results", [])[:5]:
            item = {
                "id": x["id"], "type": media_type,
                "title": x.get("title") if media_type == "movie" else x.get("name"),
                "original_title": x.get("original_title") if media_type == "movie" else x.get("original_name"),
                "year": ((x.get("release_date") or "")[:4] if media_type == "movie"
                         else (x.get("first_air_date") or "")[:4]),
                "rating": float(x.get("vote_average") or 0),
                "vote_count": int(x.get("vote_count") or 0),
                "popularity": float(x.get("popularity") or 0),
                "overview": x.get("overview") or ""}
            try:
                providers = tmdb_get(f"/{media_type}/{x['id']}/watch/providers").get("results", {})
                region_data = providers.get(region, {})
                item["netflix_available"] = any(
                    p.get("provider_name", "").lower() == "netflix"
                    for p in (region_data.get("flatrate") or []))
            except Exception:
                item["netflix_available"] = False
            found.append(enrich_with_top10(item, region))
    found.sort(key=lambda x: (x["netflix_available"], x["netflix_rank"] is not None,
                              -(x["netflix_rank"] or 999), x["rating"]), reverse=True)
    return {"query": title, "region": region, "results": found}

@app.get("/title/{media_type}/{tmdb_id}")
def title_details(media_type: str, tmdb_id: int, region: str = DEFAULT_REGION):
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type must be movie or tv")
    details = tmdb_get(f"/{media_type}/{tmdb_id}")
    providers = tmdb_get(f"/{media_type}/{tmdb_id}/watch/providers").get("results", {})
    region_data = providers.get(region.upper(), {})
    flatrate = region_data.get("flatrate") or []
    netflix_provider = next(
        (p for p in flatrate if p.get("provider_name", "").lower() == "netflix"), None)
    title = details.get("title") if media_type == "movie" else details.get("name")
    original_title = details.get("original_title") if media_type == "movie" else details.get("original_name")
    release_date = details.get("release_date") if media_type == "movie" else details.get("first_air_date")
    runtime = details.get("runtime")
    if media_type == "tv":
        runtimes = details.get("episode_run_time") or []
        runtime = runtimes[0] if runtimes else None
    result = {
        "id": tmdb_id, "type": media_type, "title": title,
        "original_title": original_title,
        "year": release_date[:4] if release_date else None,
        "rating": float(details.get("vote_average") or 0),
        "vote_count": int(details.get("vote_count") or 0),
        "popularity": float(details.get("popularity") or 0),
        "overview": details.get("overview") or "",
        "poster_path": details.get("poster_path"),
        "backdrop_path": details.get("backdrop_path"), "runtime": runtime,
        "genres": [g.get("name") for g in details.get("genres", [])],
        "production_countries": production_countries(details.get("production_countries")),
        "netflix_available": bool(netflix_provider), "netflix_provider": netflix_provider,
        "tmdb_url": f"https://www.themoviedb.org/{media_type}/{tmdb_id}"}
    return enrich_with_top10(result, region)

app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
