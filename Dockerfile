# First stage for dependencies
FROM python:3.11-slim-bookworm AS base-stage
WORKDIR /app

# Install Poetry and export dependencies to requirements.txt in one layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && curl -sSL https://install.python-poetry.org | python3 - \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /root/.local/bin/poetry /usr/local/bin/poetry

# Copy dependency files first to leverage Docker cache
COPY ./pyproject.toml ./poetry.lock ./
RUN poetry export -f requirements.txt --output requirements.txt --without-hashes

# Slim stage for final image
FROM python:3.11-slim-bookworm AS final-stage
WORKDIR /app

# Copy and install dependencies in one layer to reduce image size
COPY --from=base-stage /app/requirements.txt /app/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends \
    xauth x11-apps netpbm \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Playwright dependencies and browser binaries in a single layer
RUN pip install --no-cache-dir playwright && playwright install-deps && playwright install

# Copy the application code and set environment variables
COPY . /app
ENV PYTHONPATH="/app:$PYTHONPATH"
ENV VIDEO_PATH=/data/videos
ENV HAR_PATH=/data/har
ENV ARTIFACT_STORAGE_PATH=/data/artifacts

# Copy and set entrypoint script permissions in a single layer
COPY ./entrypoint-skyvern.sh /app/entrypoint-skyvern.sh
RUN chmod +x /app/entrypoint-skyvern.sh

# Set the entrypoint
ENTRYPOINT [ "/app/entrypoint-skyvern.sh" ]
