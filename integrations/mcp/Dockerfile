FROM python:3.11 AS requirements-stage

WORKDIR /tmp
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    ln -s /root/.local/bin/uv /usr/local/bin/uv
COPY pyproject.toml /tmp/pyproject.toml
COPY uv.lock /tmp/uv.lock
RUN uv pip compile pyproject.toml -o requirements.txt --no-annotate --no-header

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SKYVERN_API_KEY="" \
    SKYVERN_MCP_TRANSPORT=stdio \
    SKYVERN_MCP_PATH=/mcp \
    PORT=8000

WORKDIR /app

COPY --from=requirements-stage /tmp/requirements.txt /app/requirements.txt

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg && \
    rm -rf /var/lib/apt/lists/*

COPY .nvmrc /app/.nvmrc
COPY nodesource-repo.gpg.key /tmp/nodesource-repo.gpg.key
RUN mkdir -p /etc/apt/keyrings && \
    cat /tmp/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    NODE_MAJOR="$(cut -d. -f1 < /app/.nvmrc)" && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @bitwarden/cli@2025.9.0 && \
    rm -rf /var/lib/apt/lists/* /tmp/nodesource-repo.gpg.key

RUN python -m pip install --upgrade pip setuptools wheel && \
    python -m pip install -r /app/requirements.txt && \
    playwright install --with-deps chromium

COPY . /app

ENV PYTHONPATH=/app

EXPOSE 8000

# Default to stdio for Glama inspection. If Glama is configured to run the
# container over HTTP, set SKYVERN_MCP_TRANSPORT=streamable-http (or sse).
CMD ["sh", "-lc", "if [ \"${SKYVERN_MCP_TRANSPORT}\" = \"stdio\" ]; then exec python -m skyvern run mcp; else exec python -m skyvern run mcp --transport \"${SKYVERN_MCP_TRANSPORT}\" --host 0.0.0.0 --port \"${PORT}\" --path \"${SKYVERN_MCP_PATH}\"; fi"]
