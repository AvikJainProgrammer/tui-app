from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# Read at most this many leading bytes to sniff for a null byte, the usual
# tell for "this isn't text".
_BINARY_SNIFF_BYTES = 8000


class FileKind(Enum):
    TEXT = "text"
    BINARY = "binary"
    TOO_LARGE = "too_large"
    UNREADABLE = "unreadable"


@dataclass
class LoadedFile:
    kind: FileKind
    text: str = ""
    size: int = 0
    error: str = ""


def load_text_file(path: Path, max_bytes: int) -> LoadedFile:
    """Read path and classify it for display in the editor.

    Never raises: unreadable files, binary files, and oversized files are
    all reported via `.kind` rather than exceptions, since the caller needs
    to show *something* for every case rather than crash.
    """
    try:
        data = path.read_bytes()
    except OSError as e:
        return LoadedFile(kind=FileKind.UNREADABLE, error=str(e))

    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        return LoadedFile(kind=FileKind.BINARY, size=len(data))

    if len(data) > max_bytes:
        text = data[:max_bytes].decode("utf-8", errors="replace")
        return LoadedFile(kind=FileKind.TOO_LARGE, text=text, size=len(data))

    return LoadedFile(kind=FileKind.TEXT, text=data.decode("utf-8", errors="replace"), size=len(data))
