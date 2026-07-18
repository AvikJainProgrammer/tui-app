from __future__ import annotations

import json
import os
import re
from pathlib import Path

INDEX_FILENAME = ".tui_index.json"

# Skip noise directories entirely (not just their contents) so indexing a
# real project doesn't walk into node_modules/.venv/etc.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
    ".hg",
    ".svn",
}

# Don't read more than this much of any single file into the index.
MAX_INDEX_FILE_BYTES = 500_000

SEARCH_RESULT_LIMIT = 20

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> set[str]:
    return {match.group(0).lower() for match in _TOKEN_RE.finditer(text)}


def build_index(root: Path) -> dict:
    """Walk root and build a token -> [relative paths] inverted index,
    covering both file names and (for reasonably-sized text files) content.
    """
    files: list[str] = []
    tokens: dict[str, list[str]] = {}

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]

        for filename in filenames:
            if filename == INDEX_FILENAME:
                continue
            path = Path(dirpath) / filename
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                continue

            word_set = _tokenize(filename)
            try:
                if path.stat().st_size <= MAX_INDEX_FILE_BYTES:
                    data = path.read_bytes()
                    if data and b"\x00" not in data[:8000]:
                        word_set |= _tokenize(data.decode("utf-8", errors="ignore"))
            except OSError:
                pass

            files.append(rel)
            for token in word_set:
                tokens.setdefault(token, []).append(rel)

    return {"root": str(root), "files": sorted(files), "tokens": tokens}


def save_index(root: Path, index: dict) -> None:
    (root / INDEX_FILENAME).write_text(json.dumps(index))


def load_index(root: Path) -> dict | None:
    try:
        data = json.loads((root / INDEX_FILENAME).read_text())
    except (OSError, ValueError):
        return None
    if data.get("root") != str(root):
        return None
    return data


def query_index(index: dict, query: str, limit: int = SEARCH_RESULT_LIMIT) -> list[str]:
    """Relative paths matching every word in query (AND across words),
    prefix-matched against indexed tokens (OR within a word) so results
    update sensibly while the word is still being typed."""
    query_tokens = sorted(_tokenize(query))
    if not query_tokens:
        return []

    tokens_map: dict[str, list[str]] = index.get("tokens", {})
    file_hits: dict[str, int] = {}
    for query_token in query_tokens:
        matched: set[str] = set()
        for token, files in tokens_map.items():
            if token.startswith(query_token):
                matched.update(files)
        if not matched:
            return []
        for file in matched:
            file_hits[file] = file_hits.get(file, 0) + 1

    required = len(query_tokens)
    candidates = sorted(file for file, hits in file_hits.items() if hits >= required)
    return candidates[:limit]
