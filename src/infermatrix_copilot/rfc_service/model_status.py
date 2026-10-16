"""Safe model-failure state, shared by isolated chat and translation workers."""
from __future__ import annotations

from .store import encode


def safe_model_error(error):
    # Loaded only in a worker after an exception; public SDK imports stay light.
    from ..providers.zcode import ZCodeFailure
    if not isinstance(error, ZCodeFailure):
        return None
    return {"code": error.code, "message": str(error), "retryable": error.retryable, "backend": "zcode"}


def record_model_status(service, con, component, *, error=None):
    value = {"status": "failed" if error else "ready", "updated": service.clock(), "last_model_error": error}
    con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ("model_status:" + component, encode(value)))
