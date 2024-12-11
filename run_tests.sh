#!/bin/bash

source "$(poetry env info --path)/bin/activate"
poetry install

# Run pytest with various options
poetry run pytest \
    -v \
    --asyncio-mode=auto \
    "$@"  

# Exit with the pytest exit code
exit $? 