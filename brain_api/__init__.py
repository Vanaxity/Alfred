"""
Alfred Brain API
"""

__all__ = ["app", "run_server"]


def __getattr__(name):
    # Lazy on purpose: a package's __init__ always runs before any of its
    # submodules import, so an eager `from .server import app` here forced
    # even `import brain_api.auth` (stdlib-only, see brain_api/auth.py) to
    # pull in fastapi and the rest of `brain` -- packages not installed in
    # every environment this module's tests run in (confirmed missing in
    # the cloud sandbox: no fastapi/faiss/sentence-transformers there).
    if name in __all__:
        from .server import app, run_server
        return {"app": app, "run_server": run_server}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
