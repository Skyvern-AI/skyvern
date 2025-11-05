# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache \
    POETRY_HOME=/opt/poetry

# Add poetry and venv to PATH
ENV PATH="/app/.venv/bin:$POETRY_HOME/bin:$PATH"

# Set the working directory in the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # For playwright
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    # For poetry
    curl \
    # For git
    git \
    # For playwright install
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Install poetry
RUN curl -sSL https://install.python-poetry.org | python3 -

# Copy the project files into the container
COPY . .

# Install project dependencies and playwright browsers as root
RUN poetry install --no-root --no-dev \
    && playwright install --with-deps

# Create a non-root user and change ownership of the app directory
RUN addgroup --system appgroup && \
    adduser --system --ingroup appgroup appuser && \
    chown -R appuser:appgroup /app

# Switch to the non-root user
USER appuser

# Expose the port the app runs on
EXPOSE 8000

# Define the command to run the application
CMD ["poetry", "run", "uvicorn", "skyvern.app:app", "--host", "0.0.0.0", "--port", "8000"]
