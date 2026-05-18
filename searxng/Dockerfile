FROM ghcr.io/searxng/searxng:latest

# Railway wraps containers via /sbin/tini — install it since SearXNG's image doesn't ship it
ADD https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-amd64 /sbin/tini
RUN chmod +x /sbin/tini

# Install missing msgspec dependency into the SearXNG virtualenv
RUN ls /usr/local/searxng/bin/ && \
    (. /usr/local/searxng/bin/activate && pip install --no-cache-dir msgspec) || \
    /usr/local/searxng/bin/pip3 install --no-cache-dir msgspec || \
    /usr/local/searxng/bin/pip install --no-cache-dir msgspec

COPY searxng/settings.yml /etc/searxng/settings.yml
EXPOSE 8080
