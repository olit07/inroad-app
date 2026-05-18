FROM ghcr.io/searxng/searxng:latest

# Railway wraps containers via /sbin/tini — install it since SearXNG's image doesn't ship it
ADD https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-amd64 /sbin/tini
RUN chmod +x /sbin/tini

# List venv bin contents then install missing msgspec
RUN ls /usr/local/searxng/.venv/bin/ && \
    /usr/local/searxng/.venv/bin/python3 -m pip install --no-cache-dir msgspec

COPY searxng/settings.yml /etc/searxng/settings.yml
EXPOSE 8080
