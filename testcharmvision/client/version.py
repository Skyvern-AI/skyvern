from importlib import metadata

try:
    __version__ = metadata.version("testcharmvision")
except Exception:
    __version__ = "0.0.0"
