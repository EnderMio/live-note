from __future__ import annotations

LOCAL_RECOVERABLE_ACTIONS = frozenset(
    {
        "import",
        "postprocess",
        "finalize",
        "refine",
        "retranscribe",
        "merge",
        "republish",
        "resync_notes",
    }
)

REMOTE_RECOVERABLE_ACTIONS = frozenset(
    {
        "import",
        "postprocess",
        "refine",
        "retranscribe",
        "republish",
        "finalize",
    }
)
