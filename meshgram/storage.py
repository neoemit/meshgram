"""Files in the data directory that may hold secrets."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_private_json(path: Path, data: Any) -> None:
    """Replace ``path`` with ``data`` as JSON, atomically and readable only by its owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True)
    descriptor, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise
