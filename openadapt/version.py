"""Version information for the OpenAdapt meta-package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("openadapt")
except PackageNotFoundError:
    __version__ = "unknown"
