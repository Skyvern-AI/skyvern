"""What the routes package has to keep exposing as attributes (SKY-13287).

Patch targets are dotted strings that both pytest and unittest.mock resolve by walking
attributes, so anything reachable at skyvern.forge.sdk.routes.<x> has to stay reachable that way.
"""

from __future__ import annotations

import importlib
import sys

import skyvern.forge.sdk
import skyvern.forge.sdk.routes  # noqa: F401

ROUTES = "skyvern.forge.sdk.routes"
STREAMING = "skyvern.forge.sdk.routes.streaming"


def test_streaming_survives_a_re_import_that_leaves_the_subpackage_cached() -> None:
    """A fresh import binds routes.streaming through the import machinery. A re-import that
    evicts only the parent does not, because the subpackage is served from sys.modules and never
    loaded again - so routes/__init__.py has to bind the name itself."""
    original = sys.modules[ROUTES]
    del sys.modules[ROUTES]

    try:
        rebuilt = importlib.import_module(ROUTES)

        assert rebuilt is not original
        assert rebuilt.streaming is sys.modules[STREAMING]
    finally:
        sys.modules[ROUTES] = original
        skyvern.forge.sdk.routes = original
