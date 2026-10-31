# KafiaNet Movie Metadata Engine

Cloud worker for enriching the public KafiaNet poster catalog with verified TMDB metadata.

## Repositories

- Source images: `zqzqhesam11-blip/KafiaNet-Movie-Posters` (public).
- Engine code: `zqzqhesam11-blip/KafiaNet-Movie-Metadata-Engine` on `main`.
- Reverse-image service: deploy the same repository on branch `feature/kafianet-searxng-auth-20261031` as the separate Hostless app `KafiaNet-SearXNG`.
- Results are written back to the **poster repository**, so metadata commits do not trigger a redeploy of this engine.

## Safety and processing behavior

- The CLI defaults to **DRY RUN**. It never writes to GitHub unless `--apply` is supplied.
- During APPLY, source movie folders are moved into `جاهز/` or `مراجعة/` in one Git tree commit together with their JSON metadata.
- The original movie-folder name is preserved. Image filenames, extensions, and bytes are unchanged; existing Git blob SHAs are reused instead of downloading and re-uploading images.
- Every non-JSON file in a selected movie folder moves with that folder. The metadata JSON is written separately.
- A confirmed movie's JSON filename uses the best-supported worldwide public title/year. TMDB's primary title is kept unless the same alternative title is recorded in at least three distinct country markets; a tie keeps the primary title. The rule is language-neutral and never strips original-script text automatically. The chosen title and market evidence are recorded in JSON; the full original and alternative titles remain available in TMDB metadata. A movie that remains unresolved in `مراجعة/` keeps its original folder name as the JSON filename.
- Existing `مراجعة/` entries are re-scanned on later batches and can be promoted to `جاهز/` when a later match is verified. `جاهز/` and `مكرر/` are not treated as new source folders.
- A low-confidence or conflicting TinEye/TMDB match goes to `مراجعة`; it is not guessed.
- If SearXNG reports TinEye as unresponsive, the engine records the reason from `unresponsive_engines` rather than treating an upstream failure as a normal empty search. A transient empty/error response does not overwrite previously saved usable TinEye matches; those saved matches are reused and identified by `results_source`.
- Every processing run requires the separately deployed, Bearer-protected KafiaNet-SearXNG app; there is no silent fallback to title-only matching.
- Duplicate identity is based on TMDB movie ID, not title spelling.
- API keys and tokens are read only from Hostless environment variables. Never commit secrets.

## Hostless setup

Connect this repository to Hostless as a Docker-based app. The included `hostless.yaml` configures the Docker build and the `/health` HTTP health check. Scheduled runs, when needed, are managed from the Hostless dashboard.

Before enabling a scheduled run, configure the environment variables below and inspect a five-film DRY_RUN report.

Required environment variables:

- `TMDB_BEARER_TOKEN`: TMDB v3 API Bearer token.
- `SEARXNG_URL`: base URL of the separately deployed `KafiaNet-SearXNG` Hostless app.
- `SEARXNG_TOKEN`: same Bearer token configured on that SearXNG app.
- `GITHUB_TOKEN`: fine-grained token with Contents read/write on the poster repository; required only for `--apply`.
- `POSTERS_REPO`: defaults to `zqzqhesam11-blip/KafiaNet-Movie-Posters`.
- `POSTERS_BRANCH`: defaults to `main`.

Optional:

- `TMDB_LANGUAGE`: defaults to `en-US`.
- `BATCH_LIMIT`: defaults to `5`.
- `MATCH_ACCEPT_SCORE`: defaults to `0.94`.

The SearXNG app itself also needs two secrets: `SEARXNG_TOKEN` (shared with this engine app) and `SEARXNG_SECRET` (unique to SearXNG and not copied to the engine app). TinEye backlinks are used as title hints and, when present on trusted IMDb/TMDB domains, as external movie-ID evidence. Conflicting evidence is routed to review.

This product uses the TMDB API but is not endorsed or certified by TMDB.

## Local checks and commands

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m app.cli run --limit 5
```

The CLI prints a compact batch summary by default; it does not dump every movie's JSON metadata or long file-path arrays into the terminal. Use `--verbose` only when you explicitly need the full JSON report. The command above is read-only DRY RUN.


```bash
python -m app.cli run --limit 5 --apply
```

APPLY creates one atomic Git commit in the configured poster repository for each batch. Do not run it until the DRY_RUN report has been checked.

## Output layout

- `جاهز/<source-folder>/<canonical-title> (<year>).json`: high-confidence TMDB match. The source-folder name remains unchanged; the JSON filename uses the canonical title/year.
- `مراجعة/<source-folder>/<source-folder>.json`: uncertain or missing match. Its source folder and JSON filename retain the original name until resolved.
- `مكرر/<tmdb-id>/<source-folder>/<canonical-title> (<year>).json`: each source folder sharing a TMDB ID.
- `metadata/index.json`: source-folder to TMDB identity/status mapping.

All image and other non-JSON files are kept inside the corresponding movie folder when it moves. The image paths and raw image URLs in the JSON metadata and index are updated to their destination paths.
