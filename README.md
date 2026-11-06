# KafiaNet Movie Metadata Engine

Cloud worker for enriching the public KafiaNet poster catalog with verified TMDB metadata.

## Repositories and providers

- Source images: `zqzqhesam11-blip/KafiaNet-Movie-Posters` (public).
- Engine code: `zqzqhesam11-blip/KafiaNet-Movie-Metadata-Engine`.
- Reverse-image provider: Google Lens `exact_matches` tab through the Bright Data SERP API.
- Local poster OCR: RapidOCR with ONNX Runtime CPU; no external OCR API or OCR secret.
- Optional poster vision: Google Gemini image understanding (structured title/year/director candidates); enabled only when `GEMINI_API_KEY` is configured.
- Metadata results are written to the poster repository only when APPLY is explicitly requested.

## Free-tier and no-payment setup

Bright Data advertises a recurring SERP API free tier of **5,000 requests per month with no credit card required**. The allowance renews monthly. Bright Data's free-tier guidance says requests stop when the free allowance is exhausted unless the account has deposited funds.

To keep this project free:
- Create/use only the SERP API free tier.
- Do not add a payment card, deposit funds, or activate a paid plan.
- Do not configure automatic top-ups or any paid balance.
- If Bright Data reports HTTP 402 or 429, stop the batch. Do not retry repeatedly and do not add funds.
- Google Lens `exact_matches` is the reverse-image identity source. Official TMDB movie URLs and IMDb title URLs are the only external identifier inputs accepted by the automatic identity resolver.
- When Lens returns useful titles but no official TMDB/IMDb identifier, the engine also searches English Wikipedia using Lens titles only, reads the article lead for an unambiguous film year, and queries TMDB `/search/movie` using the Wikipedia title plus that year. Results and source links are saved under `wikipedia` as manual-review suggestions only; even one exact title/year result cannot set READY. Folder names and image filenames are never used.
- Separately, the poster image bytes are OCR-processed locally by RapidOCR. Up to three OCR text hints may be sent to TMDB `/search/movie`; returned rows are saved only as manual-review suggestions under `ocr.tmdb_suggestions` and are never fed into identity resolution or automatic READY decisions.
- When `GEMINI_API_KEY` is configured, the engine sends poster pixels (not its folder or filename) to Gemini only when Lens has not produced an official TMDB/IMDb ID. Gemini returns at most three visual title/year/director candidates; it never supplies an ID or URL.
- A Gemini candidate proceeds only with confidence >= 0.85, an exact TMDB title/original-title match, an exact release-year match, and either an exact director match against TMDB credits or independent title corroboration from Lens result titles. TMDB movie details must return the queried numeric ID, and the existing source-folder consistency veto still applies. Multiple matching TMDB IDs remain REVIEW. Gemini alone can never set READY.
- Gemini requests use the Interactions API with `store: false`. DRY_RUN still sends poster images to Gemini when the key is configured and consumes Google AI API quota; configure the key only when this external image analysis is desired.
- At most two bounded retries are made for transient 5xx gateway errors, transport failures, and 5xx statuses inside Bright Data's HTTP-200 response envelope. HTTP 402/429 quota responses are never retried and stop the batch.
- Bright Data free credits may be shared with other products or tools using the same account. Keep the key private.

References:
- https://brightdata.com/products/serp-api/google-search/reverse-image
- https://github.com/brightdata/cli#free-tier

**Important:** DRY_RUN does not write to the poster repository, but it does perform live Google Lens requests and consumes Bright Data free-tier requests. Start with `--limit 1`.

## Safety and matching behavior

- The CLI defaults to **DRY RUN**. It never writes to GitHub unless `--apply` is supplied.
- During APPLY, source movie folders are classified into `جاهز/` or `مراجعة/` in one Git tree commit with their JSON metadata.
- The source movie-folder name is never changed. Only the JSON basename changes to the movie title and release year returned by TMDB.
- Image filenames, extensions, bytes, and Git blob SHAs are preserved.
- Existing `مراجعة/` entries are retried on later batches. `جاهز/` and `مكرر/` are not treated as new source folders.
- Automatic movie identity is based on official TMDB movie IDs or IMDb title IDs returned by Lens and its Lens-derived official-page search, followed by TMDB `/find` and/or `/movie/{id}` verification. Local OCR text is not an identity signal.
- The engine calls TMDB `/movie/{movie_id}` for every distinct official TMDB movie ID found. A single valid ID is accepted only after TMDB returns a movie record whose `id` exactly equals the Lens ID. Distinct competing TMDB movie IDs, an unresolved ID, or no official TMDB movie ID result in REVIEW. IMDb-only links do not trigger a text or IMDb fallback.
- Movie title, release date/year, country, director, cast, overview, ratings, external IMDb ID and other metadata are read from the verified TMDB movie record. The title saved for catalog naming is the title returned by TMDB; Lens title text and country clues are not used.
- TMDB movie images are requested with `append_to_response=images` and a language fallback. Poster paths are saved with their TMDB image CDN URLs (including w500 and original sizes).
- Folder names, folder years, poster filenames, image URLs, Lens text, snippets and catalog labels are never used as movie identity inputs.
- TMDB text search is used only for OCR-derived review suggestions. Its candidates, OCR confidence, poster text, and title similarity are not accepted as proof of identity. No country search, title-similarity scoring, or metadata fingerprint comparison is used for automatic matching.
- Every READY JSON and `metadata/index.json` row records the `tmdb_id` explicitly. The `imdb_id` is also saved when TMDB supplies it; otherwise it remains null. TMDB IDs are never invented.
- A failed, blocked, or unparsed live response is recorded as an error, not as a successful empty result. Previously saved results are reusable only when their stored diagnostics explicitly identify the `exact_matches` tab; older `visual_matches` evidence is ignored.
- Each per-film terminal result reports the Lens tab, HTTP status, retry count, result count and error, the count of official TMDB movie URLs, their distinct numeric movie IDs, and the TMDB details-resolution outcome. These diagnostics do not include API secrets.
- Duplicate identity is based on TMDB movie ID, not title spelling.
- API keys and tokens are read only from Hostless environment variables. Never commit secrets.

## Local poster OCR

- Runtime: pinned `rapidocr[rtl]==3.10.0` with `onnxruntime==1.31.1` CPU inference.
- **Disabled by default:** `LOCAL_OCR_ENABLED=false`. The engine does not initialize RapidOCR or load ONNX models unless this variable is explicitly set to `true`.
- RapidOCR's ONNX models are bundled in its wheel. The Docker build initializes the OCR runtime as a build-time smoke test; this does not load the models into the running web process.
- When explicitly enabled, OCR downloads only poster pixels from the public `raw.githubusercontent.com` URL generated by the engine, enforces a 12 MiB image limit, and performs inference locally. No cloud OCR provider receives the image or text.
- OCR output and up to three TMDB text-search suggestion sets are recorded under `ocr`. These are reviewer aids only: they do not add IDs to the identity resolver, cannot produce READY, and cannot change the verified TMDB title.
- The source-folder name, poster filename, and folder year are never passed as OCR text or search inputs.
- Enable OCR only on a worker with sufficient memory and CPU headroom. The existing Hostless service allocation of 256 MiB RAM and 0.1 CPU is not considered a safe allocation for in-process OCR inference; keep the setting disabled at those limits.

## Hostless setup

Connect this repository to Hostless as a Docker-based app. The included `hostless.yaml` configures the Docker build and the `/health` HTTP health check.

Required environment variables:

- `TMDB_BEARER_TOKEN`: TMDB v3 API Bearer token.
- `BRIGHTDATA_API_KEY`: Bright Data API key, stored as a Hostless secret.
- `BRIGHTDATA_SERP_ZONE`: name of the SERP API free-tier zone created in Bright Data.
- `GITHUB_TOKEN`: fine-grained token with Contents read/write on the poster repository; required only for APPLY.
- `GEMINI_API_KEY`: optional Google AI Studio Gemini API key for poster-image recognition (stored as a Hostless secret).
- `GEMINI_MODEL`: optional model name (default `gemini-3.8-flash`).
- `POSTERS_REPO`: defaults to `zqzqhesam11-blip/KafiaNet-Movie-Posters`.
- `POSTERS_BRANCH`: defaults to `main`.


#Optional: `TMDB_LANGUAGE` (default `en-US`), `BATCH_LIMIT` (default `5`), `HTTP_TIMEOUT_SECONDS` (default `60`).

## Configuration preflight

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

The `run --limit 1` command is a live **DRY RUN** unless `--apply` is supplied. Inspect its results before processing a larger batch. When `GEMINI_API_KEY` is configured, it also sends one poster image to Gemini when Lens has no official ID. APPLY writes one Git commit per batch.

## Output layout

- `جاهز/<exact-original-source-folder>/<canonical-title> (<year>).json`: verified TMDB match.
- `مراجعة/<exact-original-source-folder>/<exact-original-source-folder>.json`: uncertain match.
- `مكرر/<tmdb-id>/<exact-original-source-folder>/<canonical-title> (<year>).json`: another source folder with the same TMDB identity.
- `metadata/index.json`: source-folder to TMDB identity/status mapping.

The metadata schema writes new provider evidence under `reverse_image` and can still read legacy `tineye` evidence saved by older runs.
