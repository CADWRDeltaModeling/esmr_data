try:
    from importlib.metadata import version, PackageNotFoundError
    __version__ = version("esmr_data")
except PackageNotFoundError:
    __version__ = "unknown"
