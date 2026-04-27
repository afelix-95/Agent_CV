"""Agent CV service package."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agent-cv")
except PackageNotFoundError:
    __version__ = "dev"
