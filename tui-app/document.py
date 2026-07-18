from __future__ import annotations

from pathlib import Path


class Document:
    """Tracks the currently open file and per-path unsaved-edit state,
    independent of any widget.

    A path is "dirty" exactly when its buffered text differs from the
    last content known to be on disk (`remember_saved`/`mark_saved`).
    Buffers persist across `open`/`close_open` so switching files and
    coming back restores in-progress edits - only `forget` (a delete) or
    quitting the app drops them.
    """

    def __init__(self) -> None:
        self.open_path: Path | None = None
        self.buffers: dict[Path, str] = {}
        self.dirty_paths: set[Path] = set()
        self._saved_reference: dict[Path, str] = {}

    def buffered_text(self, path: Path) -> str | None:
        """In-progress unsaved content for path, if any."""
        return self.buffers.get(path)

    def remember_saved(self, path: Path, text: str) -> None:
        """Record text as the known on-disk content for path, without
        touching its buffer/dirty state - used right after reading a file
        fresh from disk, before any edits have happened."""
        self._saved_reference[path] = text

    def record_change(self, path: Path, text: str) -> bool:
        """Update buffer/dirty state after the editor's content changed.

        Returns True if this changed whether path is dirty (so the caller
        knows whether any "unsaved" UI needs to refresh).
        """
        was_dirty = path in self.dirty_paths
        if text == self._saved_reference.get(path, ""):
            self.dirty_paths.discard(path)
            self.buffers.pop(path, None)
        else:
            self.buffers[path] = text
            self.dirty_paths.add(path)
        return was_dirty != (path in self.dirty_paths)

    def mark_saved(self, path: Path, text: str) -> None:
        """Record a successful save: text is now both the buffer and the
        on-disk reference, so path is no longer dirty."""
        self._saved_reference[path] = text
        self.buffers.pop(path, None)
        self.dirty_paths.discard(path)

    def open(self, path: Path) -> None:
        self.open_path = path

    def close_open(self) -> None:
        self.open_path = None

    def forget(self, path: Path, recursive: bool = False) -> bool:
        """Drop all state for path (or, if recursive, anything under it)
        after it's deleted from disk. Returns True if the currently open
        file was among them, so the caller knows to clear the editor too.
        """

        def matches(candidate: Path) -> bool:
            return candidate == path or (recursive and path in candidate.parents)

        for stale in [p for p in self.buffers if matches(p)]:
            self.buffers.pop(stale, None)
        for stale in [p for p in self.dirty_paths if matches(p)]:
            self.dirty_paths.discard(stale)
        for stale in [p for p in self._saved_reference if matches(p)]:
            self._saved_reference.pop(stale, None)

        if self.open_path is not None and matches(self.open_path):
            self.open_path = None
            return True
        return False

    def has_unsaved_under(self, path: Path) -> bool:
        """True if path itself, or anything inside it, is dirty - used to
        propagate the unsaved-change marker up to ancestor folders."""
        return any(dirty == path or path in dirty.parents for dirty in self.dirty_paths)
