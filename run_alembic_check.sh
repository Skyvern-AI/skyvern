#!/bin/sh
# first apply migrations
export PATH="${PATH}:.venv/bin"
alembic upgrade head
# then check if the database is up to date with the models
if ! alembic check; then
    echo ""
    echo "============================================"
    echo "ALEMBIC CHECK FAILED"
    echo "============================================"
    echo ""
    echo "Your database models are out of sync with the migrations."
    echo ""
    echo "If this is an OSS sync PR and you had database migrations"
    echo "in your cloud PR, you will need to generate the migration"
    echo "for this branch:"
    echo ""
    echo "  alembic revision --autogenerate -m \"your migration description\""
    echo ""
    echo "Then commit and push the generated migration file to this branch."
    echo ""
    exit 1
fi
