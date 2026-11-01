# KafiaNet Movie Metadata Engine

Cloud worker for enriching the public KafiaNet poster catalog with verified TMDB metadata.

## Repositories and providers

- Source images: `zqzqhesam11-blip/KafiaNet-Movie-Posters` (public).
- Engine code: `zqzqhesam11-blip/KafiaNet-Movie-Metadata-Engine`.
- Reverse-image provider: Google Lens through the Bright Data SERP API.
- Metadata results are written to the poster repository only when APPLY is explicitly requested.

## Free-tier and no-payment setup

Bright Data advertises a recurring SERP API free tier of **5,000 requests per month with no credit card required**. The allowance renews monthly. Bright Data's free-tier guidance says requests stop when the free allowance is exhausted unless the account has deposited funds.

To keep this project free:
- Create/use only the SERP API free tier.
- Do not add a payment card, deposit funds, or activate a paid plan.
- Do not configure automatic top-ups or any paid balance.
- If Bright Data reports HTTP 402 or 429, stop the batch. Do not retry repeatedly and do not add funds.
- Each poster uses one Google Lens image request that sends the poster URL together with the fixed refinement query `movie ID in TMDB and IMDb` in the same request. There is no separate Google text-search request and no query built from Lens titles, folder names, or image filenames.
- At most two bounded retries are made for transient 5xx gateway errors, transport failures, and 5xx statuses inside Bright Data's HTTP-200 response envelope. HTTP 402/429 quota responses are never retried and stop the batch.
- Bright Data's published Google Lens parameter list documents the image URL, language and Lens tab but does not explicitly list `q`. This implementation passes `q` as an additional Lens refinement parameter; verify its effect with a live one-poster DRY_RUN before a larger batch.
- Bright Data free credits may be shared with other products or tools using the same account. Keep the key private.

References:
- https://brightdata.com/products/serp-api/google-search/reverse-image
- https://github.com/brightdata/cli#free-tier

**Important:** DRY_RUN does not write to the poster repository, but it does perform live Google Lens requests and consumes Bright Data free-tier requests. Start with `--limit 1`.

## Safety and matching behavior

- The CLI defaults to **DRY RUN**. It never writes to GitHub unless `--apply` is supplied.
- During APPLY, source movie folders are classified into `جاهز/` or `مراجعة/` in one Git tree commit with their JSON metadata.
- The source movie-folder name is never changed. Only the JSON basename changes to the verified TMDB worldwide title and release year.
- Image filenames, extensions, bytes, and Git blob SHAs are preserved.
- Existing `مراجعة/` entries are retried on later batches. `جاهز/` and `مكرر/` are not treated as new source folders.
- **Movie identity comes only from official IMDb/TMDB page URLs in Google Lens results.** The source-folder name, folder year, Lens captions, snippets, and image filenames are never used to search TMDB or choose a movie.
- Google Lens `/goto?url=CAES...` backlinks are resolved by reading Google's `Location` header; if it is missing, one bounded redirect-following request is attempted. All page-URL candidates supplied by Lens are retained, so a broken `/goto` URL does not hide a direct official IMDb/TMDB link.
- The image search itself is refined with the fixed Lens query `movie ID in TMDB and IMDb`; no second text search is made. Lens captions and snippets remain diagnostic only and never establish identity.
- Only official IMDb/TMDB movie URLs returned by the combined image-plus-query Lens request are used to resolve IDs. TMDB movie details are fetched for the resolved TMDB candidates and the IMDb identifier is cross-checked against `external_ids.imdb_id` before READY.
- The engine resolves all distinct official IMDb/TMDB IDs from the returned result set (not just the first few). When multiple movie IDs are found, automatic selection requires one uniquely corroborated TMDB+IMDb pair; otherwise the film is sent to REVIEW.
- Every READY JSON and `metadata/index.json` row records both `tmdb_id` and `imdb_id` explicitly, in addition to TMDB's `external_ids`. If an identifier is not supplied or confirmed by the official sources, it is not invented.
- A failed, blocked, or unparsed live response is recorded as an error, not as a successful empty result. Previously saved usable reverse-image evidence remains reusable.
- Each per-film terminal result reports the Lens query phrase, HTTP status, retry count, result count and provider error. These diagnostics do not include API secrets.
- Duplicate identity is based on TMDB movie ID, not title spelling.
- API keys and tokens are read only from Hostless environment variables. Never commit secrets.

## Hostless setup

Connect this repository to Hostless as a Docker-based app. The included `hostless.yaml` configures the Docker build and the `/health` HTTP health check.

Required environment variables:

- `TMDB_BEARER_TOKEN`: TMDB v3 API Bearer token.
- `BRIGHTDATA_API_KEY`: Bright Data API key, stored as a Hostless secret.
- `BRIGHTDATA_SERP_ZONE`: name of the SERP API free-tier zone created in Bright Data.
- `GITHUB_TOKEN`: fine-grained token with Contents read/write on the poster repository; required only for APPLY.
- `POSTERS_REPO`: defaults to `zqzqhesam11-blip/KafiaNet-Movie-Posters`.
- `POSTERS_BRANCH`: defaults to `main`.

Optional: `TMDB_LANGUAGE` (default `en-US`), `BATCH_LIMIT` (default `5`), `MATCH_ACCEPT_SCORE` (default `0.94`), `HTTP_TIMEOUT_SECONDS` (default `60`).

### Configuration preflight

Run:

```bash
python -m app.cli check-brightdata-config
```

This verifies that required environment variables are present. It intentionally sends **no network request** and does not consume credits or prove that the API key/zone is active.

## Local checks

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m app.cli check-brightdata-config
python -m app.cli run --limit 1
```

The `run --limit 1` command is a live **DRY RUN** unless `--apply` is supplied. Inspect its results before processing a larger batch. APPLY writes one Git commit per batch.

## Output layout

- `جاهز/<exact-original-source-folder>/<canonical-title> (<year>).json`: verified TMDB match.
- `مراجعة/<exact-original-source-folder>/<exact-original-source-folder>.json`: uncertain match.
- `مكرر/<tmdb-id>/<exact-original-source-folder>/<canonical-title> (<year>).json`: another source folder with the same TMDB identity.
- `metadata/index.json`: source-folder to TMDB identity/status mapping.

The metadata schema writes new provider evidence under `reverse_image` and can still read legacy `tineye` evidence saved by older runs.
