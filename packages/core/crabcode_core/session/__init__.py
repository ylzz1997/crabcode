from crabcode_core.session.meta_db import SessionMetaStore
from crabcode_core.session.storage import SessionStorage, generate_session_id

__all__ = ["CoreSession", "SessionMetaStore", "SessionStorage", "generate_session_id"]


def __getattr__(name: str):
    if name == "CoreSession":
        from crabcode_core.events import CoreSession

        return CoreSession
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
