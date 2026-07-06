FROM python:3.11 AS requirements-stage
# Run `skyvern init llm` before building to generate the .env file

WORKDIR /tmp
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && ln -s /root/.local/bin/uv /usr/local/bin/uv
COPY ./pyproject.toml /tmp/pyproject.toml
COPY ./uv.lock /tmp/uv.lock
RUN uv pip compile pyproject.toml --extra server --python-version 3.11 -o requirements.txt --no-annotate --no-header

FROM python:3.11-slim-bookworm
WORKDIR /app
COPY --from=requirements-stage /tmp/requirements.txt /app/requirements.txt
COPY ./skyvern/forge/sdk/utils/tesseract_languages.py /tmp/tesseract_languages.py
RUN pip install --upgrade pip setuptools wheel
# --no-deps: requirements.txt is fully resolved by uv, including the
# pyproject overrides that loosen litellm's jsonschema==4.23.0 pin.
# Letting pip re-resolve here would re-introduce that conflict.
RUN pip install --no-cache-dir --no-deps -r requirements.txt
RUN playwright install-deps
RUN playwright install
RUN apt-get update && \
    apt-get install -y xauth x11-apps netpbm gpg ca-certificates x11vnc tesseract-ocr $(python /tmp/tesseract_languages.py --apt-packages) && \
    tesseract --version && \
    rm /tmp/tesseract_languages.py && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir websockify

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
ENV DOWNLOAD_PATH=/data/downloads
ENV BROWSER_SESSION_BASE_PATH=/data/browser_sessions
ENV LOCAL_CREDENTIAL_VAULT_PATH=/data/credential_vault

# cache tiktoken
RUN python /app/scripts/load_tiktoken.py

COPY ./entrypoint-skyvern.sh /app/entrypoint-skyvern.sh
RUN chmod +x /app/entrypoint-skyvern.sh

CMD [ "/bin/bash", "/app/entrypoint-skyvern.sh" ]
