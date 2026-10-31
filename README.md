# KafiaNet authenticated SearXNG service

This branch contains a dedicated Hostless deployment for KafiaNet's reverse-image
search endpoint. It runs the official SearXNG container and protects every route
except `GET/HEAD /health` with a Bearer token.

## Hostless deployment

Create a separate Hostless App from this same GitHub repository with:

- Repository: `zqzqhesam11-blip/KafiaNet-Movie-Metadata-Engine`
- Branch: `feature/kafianet-searxng-auth-20261031`
- App name: `KafiaNet-SearXNG`
- Working directory: `/`
- Build system: Docker
- Dockerfile: `Dockerfile`
- Hostless file: `hostless.yaml`
- Health check: HTTP `/health`, every 20 seconds

Do not select `main` for this service: this branch intentionally contains the
SearXNG container as its repository root. The metadata engine remains on `main`.

## Required Hostless environment variables

Generate two independent high-entropy secrets in Hostless:

- `SEARXNG_TOKEN`: Bearer token required by the gateway on all non-health routes.
- `SEARXNG_SECRET`: SearXNG Flask/session secret; the official container maps this
  value into the SearXNG settings.

Never commit either value or paste it into chat. Use the same `SEARXNG_TOKEN`
value in the metadata engine app as its `SEARXNG_TOKEN`.

## Endpoint contract

- Health: `GET /health` returns HTTP 200 without authentication.
- Search: `GET /search?q=<public-image-url>&engines=tineye&format=json` requires
  `Authorization: Bearer <SEARXNG_TOKEN>`.
- JSON output is enabled in SearXNG settings.
- TinEye is enabled from the stock SearXNG engine configuration.

The TinEye engine is a URL-search engine: the query must be a publicly reachable
image URL. It is not an official TinEye API integration.

## Security notes

- The wrapper checks the Authorization header using constant-time comparison.
- No token is stored in Git.
- All SearXNG pages and API routes are protected, not just `/search`.
- Hostless health probes are the only unauthenticated route.
