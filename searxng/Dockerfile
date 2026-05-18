FROM ghcr.io/searxng/searxng:latest

# Railway wraps containers via /sbin/tini — install it since SearXNG's image doesn't ship it
ADD https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-amd64 /sbin/tini
RUN chmod +x /sbin/tini

# Find Python and install missing msgspec dependency
RUN find /usr -name "python3" -o -name "python" 2>/dev/null | head -5
RUN find /usr -name "pip3" -o -name "pip" 2>/dev/null | head -5

COPY searxng/settings.yml /etc/searxng/settings.yml
EXPOSE 8080
