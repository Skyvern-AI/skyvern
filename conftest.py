from skyvern.forge.sdk.forge_log import setup_logger

# After SKY-7947 (lightweight Skyvern wheel), `import skyvern` no longer
# configures structlog as a side effect. Without this call the default
# rich-based exception renderer is active during tests, which raises
# `TypeError: 'Mock' object is not iterable` when it tries to introspect
# Mock locals in tracebacks (e.g. utils_test.py::TestParseApiResponse).
setup_logger()
