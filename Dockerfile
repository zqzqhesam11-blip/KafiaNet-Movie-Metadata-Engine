FROM searxng/searxng:latest

# Keep SearXNG's official startup script (settings setup, permissions and
# environment handling), but replace its final Granian target with our
# WSGI authentication wrapper.
USER root
WORKDIR /usr/local/searxng

COPY --chown=977:977 settings.yml /etc/searxng/settings.yml
COPY --chown=977:977 auth_middleware.py /usr/local/searxng/auth_middleware.py
COPY --chown=977:977 kafianet_gateway.py /usr/local/searxng/kafianet_gateway.py

RUN set -eu; \
    grep -Fq 'exec /usr/local/searxng/.venv/bin/granian searx.webapp:app' /usr/local/searxng/entrypoint.sh; \
    sed -i 's#exec /usr/local/searxng/.venv/bin/granian searx.webapp:app#export GRANIAN_HOST=0.0.0.0; export GRANIAN_PORT="${PORT:-8080}"; exec /usr/local/searxng/.venv/bin/granian kafianet_gateway:app#' /usr/local/searxng/entrypoint.sh; \
    chmod 0644 /etc/searxng/settings.yml /usr/local/searxng/auth_middleware.py /usr/local/searxng/kafianet_gateway.py

ENV SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml
EXPOSE 8080

USER 977:977
