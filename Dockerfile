FROM python:3.11 as requirements-stage

WORKDIR /tmp
RUN pip install poetry
COPY ./pyproject.toml /tmp/pyproject.toml
COPY ./poetry.lock /tmp/poetry.lock
RUN poetry export -f requirements.txt --output requirements.txt --without-hashes

FROM python:3.11-slim-bookworm
WORKDIR /app
COPY --from=requirements-stage /tmp/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade -r requirements.txt
RUN playwright install-deps
RUN playwright install
RUN apt-get install -y xauth x11-apps netpbm && apt-get clean

# Add these lines to install dos2unix and convert entrypoint scripts
RUN apt-get update && \
    apt-get install -y dos2unix && \
    apt-get clean

COPY . /app

# Convert line endings
RUN dos2unix /app/entrypoint-skyvern.sh && \
    chmod +x /app/entrypoint-skyvern.sh

ENV PYTHONPATH="/app:$PYTHONPATH"
ENV VIDEO_PATH=/data/videos
ENV HAR_PATH=/data/har
ENV LOG_PATH=/data/log
ENV ARTIFACT_STORAGE_PATH=/data/artifacts

CMD [ "/bin/bash", "/app/entrypoint-skyvern.sh" ]
