"""Load a local operator credential without putting it in a command line."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def load_token(token_file: Path | None, token_env: str) -> str:
    if token_file is None:
        token = os.environ.get(token_env, "")
        if not token:
            raise RuntimeError(f"token environment variable {token_env} is empty")
        return token

    path = token_file.expanduser()
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() \
                    or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise RuntimeError("operator token file must be an owned private regular file")
            raw = source.read(4097)
    except OSError as error:
        raise RuntimeError("operator token file is unavailable") from error
    if not 1 <= len(raw) <= 4096:
        raise RuntimeError("operator token file size is invalid")
    try:
        token = raw.rstrip(b"\r\n").decode("ascii")
    except UnicodeDecodeError as error:
        raise RuntimeError("operator token file encoding is invalid") from error
    if not token or any(character.isspace() for character in token):
        raise RuntimeError("operator token file content is invalid")
    return token
