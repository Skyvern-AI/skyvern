FROM python:3.11 AS requirements-stage
# Run `skyvern init llm` before building to generate the .env file

WORKDIR /tmp
RUN pip install poetry
RUN poetry self add poetry-plugin-export
COPY ./pyproject.toml /tmp/pyproject.toml
COPY ./poetry.lock /tmp/poetry.lock
RUN poetry export -f requirements.txt --output requirements.txt --without-hashes

FROM python:3.11-slim-bookworm
WORKDIR /app
COPY --from=requirements-stage /tmp/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir --upgrade -r requirements.txt
RUN playwright install-deps
RUN playwright install
RUN apt-get install -y xauth x11-apps netpbm curl && apt-get clean

COPY .nvmrc /app/.nvmrc
# Install Node.js based on .nvmrc version (without nvm)
RUN NODE_MAJOR=$(cut -d. -f1 < /app/.nvmrc) && \
    curl --fail --silent --show-error --location https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean

# confirm installation
RUN npm -v && node -v
# install bitwarden cli
RUN npm install -g @bitwarden/cli@2024.9.0
# checking bw version also initializes the bw config
RUN bw --version

COPY . /app

ENV PYTHONPATH="/app"
ENV VIDEO_PATH=/data/videos
ENV HAR_PATH=/data/har
ENV LOG_PATH=/data/log
ENV ARTIFACT_STORAGE_PATH=/data/artifacts

COPY ./entrypoint-skyvern.sh /app/entrypoint-skyvern.sh
RUN chmod +x /app/entrypoint-skyvern.sh

CMD [ "/bin/bash", "/app/entrypoint-skyvern.sh" ]
