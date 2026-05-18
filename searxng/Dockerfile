FROM ghcr.io/searxng/searxng:latest

# Railway wraps containers via /sbin/tini — install it since SearXNG's image doesn't ship it
ADD https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-amd64 /sbin/tini
RUN chmod +x /sbin/tini

# Locate Python and pip then install missing msgspec
RUN find / -name "pip3" -o -name "pip" -o -name "python3" -o -name "python" 2>/dev/null | grep -v proc | head -20

COPY searxng/settings.yml /etc/searxng/settings.yml
EXPOSE 8080
