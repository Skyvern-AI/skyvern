FROM python:3.11 AS requirements-stage
# Run `skyvern init llm` before building to generate the .env file

WORKDIR /tmp
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -s /root/.local/bin/uv /usr/local/bin/uv
COPY ./pyproject.toml /tmp/pyproject.toml
COPY ./uv.lock /tmp/uv.lock
RUN uv pip compile pyproject.toml -o requirements.txt --no-annotate --no-header

FROM python:3.11-slim-bookworm
WORKDIR /app
COPY --from=requirements-stage /tmp/requirements.txt /app/requirements.txt
RUN pip install --upgrade pip setuptools wheel
RUN pip install --no-cache-dir --upgrade -r requirements.txt
RUN playwright install-deps
RUN playwright install
RUN apt-get install -y xauth x11-apps netpbm gpg ca-certificates && apt-get clean

COPY .nvmrc /app/.nvmrc
COPY nodesource-repo.gpg.key /tmp/nodesource-repo.gpg.key
RUN cat /tmp/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    NODE_MAJOR=$(cut -d. -f1 < /app/.nvmrc) && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" >> /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    rm /tmp/nodesource-repo.gpg.key && \
    # confirm installation
    npm -v && node -v


# install bitwarden cli
RUN npm install -g @bitwarden/cli@2025.9.0
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
