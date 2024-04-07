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
RUN pip install --no-cache-dir streamlit
RUN playwright install-deps
RUN playwright install
RUN apt-get install -y xauth && apt-get clean

COPY . /app

ENV PYTHONPATH="/app:$PYTHONPATH"
ENV VIDEO_PATH=/data/videos
ENV HAR_PATH=/data/har
ENV ARTIFACT_STORAGE_PATH=/data/artifacts

COPY ./entrypoint-skyvern.sh /app/entrypoint-skyvern.sh
RUN chmod +x /app/entrypoint-skyvern.sh

COPY ./entrypoint-streamlit.sh /app/entrypoint-streamlit.sh
RUN chmod +x /app/entrypoint-streamlit.sh

CMD [ "/bin/bash", "/app/entrypoint-skyvern.sh" ]



