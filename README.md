# Netflix Finder v1

A small API for a ChatGPT/AI action that finds Netflix titles by:

- production country
- movie / series / miniseries
- minimum rating
- minimum vote count
- rating / vote count / popularity sorting
- Netflix availability in a country (default: Denmark)

## Data sources

TMDB is used for title metadata, ratings, vote counts and Netflix availability.
TMDB states that its watch-provider availability data is powered by a partnership with JustWatch and requires JustWatch attribution.

Netflix's official weekly Top 10 / viewing data is intentionally a separate integration in v1 because it is not a stable catalog API. The next iteration can add a Netflix Views endpoint/cache.

## Setup

1. Create a TMDB account and request an API Read Access Token.
2. Copy `.env.example` to `.env`.
3. Put the token in `TMDB_TOKEN`.
4. Run:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open `/docs`.

## Example

GET:

`/search?production_country=KR&content_type=series&min_rating=7.5&min_votes=10000&sort=rating&region=DK&limit=10`

This means:
- Korean production
- series
- currently available on Netflix in Denmark
- rating >= 7.5
- at least 10,000 votes
- highest rated first

## ChatGPT integration

Expose `/openapi.json` over HTTPS and import that OpenAPI schema into the action/app builder used by your ChatGPT project.

The action can then translate natural language such as:

"Znajdź 10 najlepiej ocenianych koreańskich seriali na Netflixie w Danii, minimum 7.5 i 10 tys. ocen."

into the `/search` parameters.
