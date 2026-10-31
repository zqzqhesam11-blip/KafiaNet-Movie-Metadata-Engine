from fastapi import FastAPI

app = FastAPI(
    title="KafiaNet Movie Metadata Engine",
    version="0.1.0",
    description="Cloud metadata enrichment for the KafiaNet public poster catalog.",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "kafianet-movie-metadata-engine"}


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "KafiaNet Movie Metadata Engine",
        "mode": "read-only until the batch CLI is explicitly run with --apply",
        "health": "/health",
    }
