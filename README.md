# KafiaNet Movie Metadata Engine

Cloud worker for enriching the public KafiaNet poster catalog with verified TMDB metadata.

## Repositories and providers

- Source images: `zqzqhesam11-blip/KafiaNet-Movie-Posters` (public).
- Engine code: `zqzqhesam11-blip/KafiaNet-Movie-Metadata-Engine`.
- Reverse-image provider: Google Lens `exact_matches` tab through the Bright Data SERP API.
- Local poster OCR: RapidOCR with ONNX Runtime CPU; no external OCR API or OCR secret.
- Metadata results are written to the poster repository only when APPLY is explicitly requested.

## Free-tier and no-payment setup

Bright Data advertises a recurring SERP API free tier of **5,000 requests per month with no credit card required**. The allowance renews monthly. Bright Data's free-tier guidance says requests stop when the free allowance is exhausted unless the account has deposited funds.

To keep this project free:
- Create/use only the SERP API free tier.
- Do not add a payment card, deposit funds, or activate a paid plan.
- Do not configure automatic top-ups or any paid balance.
- If Bright Data reports HTTP 402 or 429, stop the batch. Do not retry repeatedly and do not add funds.
- Google Lens `exact_matches` is the reverse-image discovery source. Official TMDB movie URLs and IMDb title URLs returned by Lens are candidate identifiers; TMDB alone verifies the final numeric movie ID and metadata.
- The identity path uses Google Lens and, when Lens has no official ID, one bounded Google site-scoped title search for official IMDb/TMDB movie pages. TMDB `/find` and `/movie/{id}` verify any selected identifier.
- Optional reviewer-only research from Wikipedia, Wikidata, and TMDB text suggestions is **disabled by default** (`REVIEW_RESEARCH_ENABLED=false`). It adds serial network requests but cannot establish identity or authorize READY; enable it only for manual investigation.
- AFI Catalog and BFI National Archive references may be captured from the title-scoped search as reviewer context. They are not proof of identity and have specialized, incomplete coverage.
- Wikipedia, Wikidata, AFI, and BFI results always use `identity_evidence=false` and `automatic_match_allowed=false`. Only an official TMDB/IMDb identifier resolved and verified through TMDB can authorize READY. Folder names and image filenames are never used as search inputs.
- Separately, the poster image bytes are OCR-processed locally by RapidOCR. Up to three OCR text hints may be sent to TMDB `/search/movie`; returned rows are saved only as manual-review suggestions under `ocr.tmdb_suggestions` and are never fed into identity resolution or automatic READY decisions.
- At most two bounded retries are made for transient 5xx gateway errors, transport failures, and 5xx statuses inside Bright Data's HTTP-200 response envelope. HTTP 402/429 quota responses are never retried and stop the batch.
- Bright Data free credits may be shared with other products or tools using the same account. Keep the key private.

References:
- Google Lens through Bright Data: https://brightdata.com/products/serp-api/google-search/reverse-image
- Wikidata Action API: https://www.wikidata.org/w/api.php
- AFI Catalog: https://catalog.afi.com/
- BFI National Archive Collections Search: https://collections-search.bfi.org.uk/
- Wikipedia MediaWiki API: https://www.mediawiki.org/wiki/API:Search
- TMDB API documentation: https://developer.themoviedb.org/docs
- Bright Data free tier: https://github.com/brightdata/cli#free-tier

**Important:** DRY_RUN does not write to the poster repository, but it does perform live Google Lens requests and consumes Bright Data free-tier requests. Start with `--limit 1`.

## Safety and matching behavior

- The CLI defaults to **DRY RUN**. It never writes to GitHub unless `--apply` is supplied.
- During APPLY, source movie folders are classified into `جاهز/` or `مراجعة/` in one Git tree commit with their JSON metadata.
- The source movie-folder name is never changed. Only the JSON basename changes to the movie title and release year returned by TMDB.
- Image filenames, extensions, bytes, and Git blob SHAs are preserved.
- Existing `مراجعة/` entries are retried on later batches. `جاهز/` and `مكرر/` are not treated as new source folders.
- Automatic movie identity can come only from official TMDB/IMDb IDs returned by Google Lens or the Lens-derived search, then independently resolved and verified through TMDB. Local OCR, Wikipedia, Wikidata, AFI, and BFI are reviewer aids only.
- The engine calls TMDB `/movie/{movie_id}` for every distinct candidate ID. A selected ID is accepted only after TMDB returns the same numeric ID and, when an IMDb ID was used, confirms the same IMDb external ID. When a title-scoped Google search returns multiple official IDs, one candidate may be selected only if its verified TMDB title (or official alternative title) and year exactly match the primary Lens title/year and no other candidate matches. Unresolved IDs returned directly by Lens and competing direct Lens IDs still result in REVIEW. Lens title text can disambiguate verified official IDs; it can never create an ID or independently authorize READY.
- Movie title, release date/year, country, director, cast, overview, ratings, external IMDb ID and other metadata are read from the verified TMDB movie record. The title saved for catalog naming is the title returned by TMDB; Lens title text and country clues are not used.
- TMDB movie images are requested with `append_to_response=images` and a language fallback. Poster paths are saved with their TMDB image CDN URLs (including w500 and original sizes).
- Folder names, folder years, poster filenames, image URLs, and catalog labels are never used to discover or positively select a movie. The folder label remains a negative-only veto, and blocks only when both its explicit title and explicit year conflict with the already-verified TMDB record. Local-language titles, transliterations, or a missing folder year alone do not force REVIEW.
- TMDB text search is used for Wikipedia and OCR reviewer suggestions. Wikidata, AFI, and BFI are additional research references only; loose title similarity, country clues, and folder names are never used to select a movie.
- Each JSON record stores Wikipedia results under `wikipedia`, Wikidata cross-checks under `wikidata`, and AFI/BFI references under `catalog_sources`. The matching summary reports `wikidata_status`, `wikidata_candidates`, `afi_catalog_results`, and `bfi_archive_results`.
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
- `POSTERS_REPO`: defaults to `zqzqhesam11-blip/KafiaNet-Movie-Posters`.
- `POSTERS_BRANCH`: defaults to `main`.


#Optional: `TMDB_LANGUAGE` (default `en-US`), `BATCH_LIMIT` (default `5`), `HTTP_TIMEOUT_SECONDS` (default `60`), and `REVIEW_RESEARCH_ENABLED` (default `false`).

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

The run command defaults to **DRY RUN** and does not write or move catalog files. It still performs live Lens/Bright Data requests and, when needed, a Google title search and TMDB verification. Wikipedia/Wikidata reviewer research adds extra requests only when `REVIEW_RESEARCH_ENABLED=true`. Live requests may consume provider quotas.

## Deterministic DRY_RUN / APPLY

A normal `--apply` performs new live searches. To ensure APPLY uses the exact identity and evidence reviewed during DRY_RUN, save a local plan and apply that same plan:

```bash
python -u -m app.cli run --limit 5 --plan-out /tmp/kafianet-plan-001.json
python -u -m app.cli run --apply --apply-plan /tmp/kafianet-plan-001.json
```

The plan contains the exact Lens/Google results, verified TMDB details, source image blob SHAs, and the poster-repository HEAD. APPLY checks that the repository and every selected image are unchanged, then writes the saved decisions without making Lens, Google Search, or TMDB requests. A successful commit consumes and deletes the local plan file. Keep both commands in the same running container because the plan file is local, not stored in the public poster repository.

Previously processed posters with an unchanged image SHA keep their saved READY/REVIEW decision and do not trigger another search. Use `--refresh-evidence` only when you intentionally want a new lookup for unchanged REVIEW posters. A changed poster image is detected by its Git blob SHA and is processed as new evidence.


- `جاهز/<exact-original-source-folder>/<canonical-title> (<year>).json`: verified TMDB match.
- `مراجعة/<exact-original-source-folder>/<exact-original-source-folder>.json`: uncertain match.
- `مكرر/<tmdb-id>/<exact-original-source-folder>/<canonical-title> (<year>).json`: another source folder with the same TMDB identity.
- `metadata/index.json`: source-folder to TMDB identity/status mapping.

The metadata schema writes new provider evidence under `reverse_image` and can still read legacy `tineye` evidence saved by older runs.
