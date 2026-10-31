# KafiaNet Movie Metadata Engine

Cloud worker for enriching the public KafiaNet poster catalog with verified TMDB metadata.

## Repositories and providers

- Source images: `zqzqhesam11-blip/KafiaNet-Movie-Posters` (public).
- Engine code: `zqzqhesam11-blip/KafiaNet-Movie-Metadata-Engine` on `main`.
- Reverse-image search: official TinEye REST API over HTTPS using the `X-API-KEY` header, called directly by the engine.
- Metadata results are written to the poster repository.

## Safety and processing behavior

- The CLI defaults to **DRY RUN**. It never writes to GitHub unless `--apply` is supplied.
- During APPLY, source movie folders move into `جاهز/` or `مراجعة/` in one Git tree commit with their JSON metadata.
- The source movie-folder name is never changed. Only the JSON basename changes to the verified TMDB worldwide title and release year.
- Image filenames, extensions, bytes, and Git blob SHAs are preserved.
- Existing `مراجعة/` entries are retried on later batches. `جاهز/` and `مكرر/` are not treated as new source folders.
- **Mandatory title matching applies to every movie.** Before a result can become READY, its TMDB title or original title must match either a source-folder title variant or a meaningful TinEye poster filename/title. A trusted IMDb/TMDB ID alone is never sufficient.
- TinEye backlinks are retained with their associated IMDb/TMDB IDs and available image-filename evidence. When TMDB candidates share the same title/year, the engine compares production country, director, runtime, and release metadata.
- TinEye errors are reported with actionable API-specific reasons: invalid image URL (400), invalid API key (401), exhausted bundle (402), denied access (403), rate limiting (429), and provider/network failures. Failed live results do not overwrite previously saved usable evidence.
- Duplicate identity is based on TMDB movie ID, not title spelling.
- API keys and tokens are read only from Hostless environment variables. Never commit secrets.

## Hostless setup

Connect this repository to Hostless as a Docker-based app. The included `hostless.yaml` configures the Docker build and the `/health` HTTP health check.

Required environment variables:

- `TMDB_BEARER_TOKEN`: TMDB v3 API Bearer token.
- `TINEYE_API_KEY`: official TinEye API key, stored as a Hostless secret. Real searches require an active paid search bundle.
- `GITHUB_TOKEN`: fine-grained token with Contents read/write on the poster repository; required only for `--apply`.
- `POSTERS_REPO`: defaults to `zqzqhesam11-blip/KafiaNet-Movie-Posters`.
- `POSTERS_BRANCH`: defaults to `main`.

Optional: `TMDB_LANGUAGE` (default `en-US`), `BATCH_LIMIT` (default `5`), `MATCH_ACCEPT_SCORE` (default `0.94`), `HTTP_TIMEOUT_SECONDS` (default `60`).

### Verify TinEye before processing

Run `python -m app.cli check-tineye`. It calls TinEye's `remaining_searches` endpoint, verifies the API key, and reports available credits without consuming an image-search credit. Each film lookup consumes one search. Ensure the bundle covers the planned batch.

Do not use TinEye's public sandbox key for catalog processing: it returns demonstration results, not results for the submitted poster.

This product uses the TMDB API but is not endorsed or certified by TMDB.

## Local checks

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m app.cli check-tineye
python -m app.cli run --limit 5
```

APPLY writes one Git commit per batch. Inspect DRY_RUN results before using `--apply`.

## Output layout

- `جاهز/<exact-original-source-folder>/<canonical-title> (<year>).json`: verified TMDB match.
- `مراجعة/<exact-original-source-folder>/<exact-original-source-folder>.json`: uncertain match.
- `مكرر/<tmdb-id>/<exact-original-source-folder>/<canonical-title> (<year>).json`: another source folder with the same TMDB identity.
- `metadata/index.json`: source-folder to TMDB identity/status mapping.
