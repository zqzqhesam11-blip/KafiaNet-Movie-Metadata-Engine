# KafiaNet Movie Metadata Engine

Cloud worker for enriching the public KafiaNet poster catalog with verified TMDB metadata.

## Repositories and providers

- Source images: `zqzqhesam11-blip/KafiaNet-Movie-Posters` (public).
- Engine code: `zqzqhesam11-blip/KafiaNet-Movie-Metadata-Engine`.
- Reverse-image provider: the official TinEye REST API over HTTPS using the `X-API-KEY` header.
- Metadata and poster organization are written to the poster repository only when APPLY is explicitly requested.

TinEye is used to find the same image and altered copies (for example, resized, cropped, or edited versions). It is not a semantic image-understanding system: a matching subject or similar-looking poster is not proof that two images represent the same film.

## TinEye API and search-credit safety

- Configure `TINEYE_API_KEY` as a Hostless secret/environment variable. Never commit the key or paste it into chat.
- Real TinEye image searches require an active paid search bundle. Each poster lookup consumes TinEye search credit.
- The engine does not purchase credits, add a payment method, enable top-ups, or retry exhausted searches.
- Run `python -m app.cli check-tineye` before processing. It calls TinEye's `remaining_searches` endpoint to verify access and report remaining credits; it does not submit a poster image or consume an image-search credit.
- HTTP/API authentication, permission, quota, throttling, server, or network failures stop the remaining batch when continuing would waste credits or repeat a provider-wide failure.
- **Important:** DRY_RUN does not write to the poster repository, but it does perform live TinEye image searches and consumes paid TinEye credits. Begin with `python -m app.cli run --limit 1`.

## Safety and matching behavior

- The CLI defaults to **DRY RUN**. It never writes to GitHub unless `--apply` is supplied.
- During APPLY, source movie folders are classified into `جاهز/` or `مراجعة/` in one Git tree commit with their JSON metadata.
- The source movie-folder name is never changed. Only the JSON basename changes to the verified TMDB worldwide title and release year.
- Image filenames, extensions, bytes, and Git blob SHAs are preserved.
- Existing `مراجعة/` entries are retried on later batches. `جاهز/` and `مكرر/` are not treated as new source folders.
- Automatic movie identity is resolved only from official TMDB movie URLs or IMDb title URLs returned as TinEye backlinks. IMDb IDs are mapped through TMDB `/find`; every candidate is then checked against TMDB movie details. A TMDB movie ID is accepted only when the returned details contain the same ID.
- Distinct competing official TMDB IDs, unresolved IMDb IDs, or failed TMDB detail verification result in REVIEW. TinEye similarity scores, matched-image filenames, snippets, and page text do not independently establish a film identity.
- TinEye-returned matched-image names may be extracted as title hints for manual-review assistance. They are never treated as official identifiers and cannot set READY.
- When official TinEye backlinks do not identify a movie, the engine may search English Wikipedia from TinEye-returned matched-image title hints, read an unambiguous film year, and query TMDB `/search/movie` using the Wikipedia article title plus that year. All these Wikipedia/TMDB results are stored as reviewer suggestions only; even an exact title/year match cannot set READY.
- Source-folder names, source-folder years, local poster filenames, local path names, and guessed titles are never used as movie identity inputs or as search inputs.
- TMDB supplies the canonical title, release date/year, country, director, cast, overview, ratings, external IMDb ID, and other saved metadata. The title used for the JSON basename comes from the verified TMDB movie record.
- TMDB images are requested with `append_to_response=images` and language fallback. Poster paths are saved with their TMDB image CDN URLs (including w500 and original sizes).
- Every READY JSON and `metadata/index.json` row records the `tmdb_id` explicitly. The `imdb_id` is also saved when TMDB supplies it; otherwise it remains null. TMDB IDs are never invented.
- Failed, blocked, or unparsed TinEye responses are recorded as errors, not successful empty searches. Previously saved TinEye REST API evidence and explicitly tagged Google Lens `exact_matches` evidence can be reused for the same poster; old scraped/SearXNG TinEye rows and Google Lens `visual_matches` evidence are not reused.
- Per-film diagnostics include the provider, HTTP/API status, match/backlink counts, official TMDB/IMDb URLs and IDs, TMDB resolution result, and errors. API secrets are never logged.
- Duplicate identity is based on TMDB movie ID, not title spelling.

## Local poster OCR

- Runtime: pinned `rapidocr[rtl]==3.10.0` with `onnxruntime==1.31.1` CPU inference.
- **Disabled by default:** `LOCAL_OCR_ENABLED=false`. The engine does not initialize RapidOCR or load ONNX models unless this variable is explicitly set to `true`.
- OCR output and any TMDB text-search results are reviewer aids only. They do not add IDs to the identity resolver, cannot produce READY, and cannot change the verified TMDB title.
- The source-folder name, poster filename, and folder year are never passed as OCR text or search inputs.
- Enable OCR only on a worker with sufficient memory and CPU headroom. The existing Hostless allocation of 256 MiB RAM and 0.1 CPU is not considered safe for in-process OCR inference; keep it disabled at those limits.

## Hostless setup

Connect this repository to Hostless as a Docker-based app. The included `hostless.yaml` configures the Docker build and the `/health` HTTP health check.

Required environment variables:

- `TMDB_BEARER_TOKEN`: TMDB v3 API Bearer token.
- `TINEYE_API_KEY`: official TinEye API key, stored as a Hostless secret.
- `GITHUB_TOKEN`: fine-grained token with Contents read/write on the poster repository; required only for APPLY.
- `POSTERS_REPO`: defaults to `zqzqhesam11-blip/KafiaNet-Movie-Posters`.
- `POSTERS_BRANCH`: defaults to `main`.

Optional: `TMDB_LANGUAGE` (default `en-US`), `BATCH_LIMIT` (default `5`), `HTTP_TIMEOUT_SECONDS` (default `60`), `LOCAL_OCR_ENABLED` (default `false`).

### Verify TinEye before processing

Run:

```bash
python -m app.cli check-tineye
```

This verifies the API key and reports available search credits without uploading or searching a poster image.

## Local checks

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m app.cli check-tineye
python -m app.cli run --limit 1
```

The `run --limit 1` command is a live **DRY RUN** unless `--apply` is supplied. Inspect the report before processing a larger batch. APPLY writes one Git commit per batch.

## Output layout

- `جاهز/<exact-original-source-folder>/<canonical-title> (<year>).json`: verified TMDB match.
- `مراجعة/<exact-original-source-folder>/<exact-original-source-folder>.json`: uncertain match.
- `مكرر/<tmdb-id>/<exact-original-source-folder>/<canonical-title> (<year>).json`: another source folder with the same TMDB identity.
- `metadata/index.json`: source-folder to TMDB identity/status mapping.

The metadata schema stores current reverse-image provider evidence under `reverse_image` and supports explicitly recognized older `tineye` evidence without changing the original poster files.
