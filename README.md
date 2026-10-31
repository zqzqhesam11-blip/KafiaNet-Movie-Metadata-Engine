# KafiaNet Movie Metadata Engine

Cloud worker for enriching the public KafiaNet poster catalog with verified TMDB metadata.

## Repositories

- Source images: `zqzqhesam11-blip/KafiaNet-Movie-Posters` (public).
- Engine code: `zqzqhesam11-blip/KafiaNet-Movie-Metadata-Engine`.
- Results are written back to the **poster repository**, so metadata commits do not trigger a redeploy of this engine.

## Safety defaults

- The CLI defaults to **DRY RUN**. It never writes to GitHub unless `--apply` is supplied.
- It does not rename, move, delete, or rewrite poster image folders.
- A low-confidence or conflicting TinEye/TMDB match goes to `مراجعة`; it is not guessed.
- Every processing run requires the separately deployed, Bearer-protected KafiaNet-SearXNG app; there is no silent fallback to title-only matching.
- Duplicate identity is based on TMDB movie ID, not title spelling.
- API keys and tokens are read only from Hostless environment variables. Never commit secrets.

## Hostless setup

Connect this repository to Hostless as a Python App. The included `hostless.yaml` configures the API health endpoint and a paused test cron job. Do not resume the cron job until the environment variables below are configured and a five-film DRY_RUN is reviewed.

Required environment variables:

- `TMDB_BEARER_TOKEN`: TMDB v3 API Bearer token.
- `SEARXNG_URL`: base URL of the separately deployed `KafiaNet-SearXNG` Hostless app.
- `SEARXNG_TOKEN`: same Bearer token configured on that SearXNG app.
- `GITHUB_TOKEN`: fine-grained token with Contents read/write on the poster repository; required only for `--apply`.
- `POSTERS_REPO`: defaults to `zqzqhesam11-blip/KafiaNet-Movie-Posters`.
- `POSTERS_BRANCH`: defaults to `main`.

Optional:

- `TMDB_LANGUAGE`: defaults to `en-US`.

The SearXNG app itself also needs two secrets: `SEARXNG_TOKEN` (shared with this engine app) and `SEARXNG_SECRET` (unique to SearXNG and not copied to the engine app). TinEye backlinks are used as title hints and, when present on trusted IMDb/TMDB domains, as external movie-ID evidence. Conflicting evidence is routed to review.

This product uses the TMDB API but is not endorsed or certified by TMDB.
- `BATCH_LIMIT`: defaults to `5`.
- `MATCH_ACCEPT_SCORE`: defaults to `0.94`.

## Local checks

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m app.cli run --limit 5
```

The command above is DRY RUN. To write metadata, use `python -m app.cli run --limit 5 --apply`.

## Output layout

- `جاهز/<source-folder>/movie.json`: high-confidence TMDB match.
- `مكرر/<tmdb-id>/<source-folder>/movie.json`: each source folder sharing a TMDB ID.
- `مراجعة/<source-folder>/movie.json`: uncertain or missing match.
- `metadata/index.json`: source-folder to TMDB identity/status mapping.

Existing poster folders and images are preserved during the metadata phase. Folder rename plans are recorded in JSON and must be reviewed separately before any rename operation.
