#!/bin/sh
# first apply migrations
export PATH="${PATH}:.venv/bin"
alembic upgrade head
# then check if the database is up to date with the models
alembic check
