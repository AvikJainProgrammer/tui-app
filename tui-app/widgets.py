from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from textual.binding import Binding
from textual.suggester import Suggester
from textual.widgets import DirectoryTree, Input, Label, ListItem, ListView, Static

from document import Document

if TYPE_CHECKING:
    from app import LayoutApp


class RightPanel(Static):
    """Right sidebar panel."""


class FileTree(DirectoryTree):
    """DirectoryTree with new-file/new-folder shortcuts and unsaved markers."""

    BINDINGS = [
        Binding("n", "new_file", "New File"),
        Binding("N", "new_directory", "New Folder"),
    ]

    def __init__(self, path: str, document: Document, **kwargs) -> None:
        super().__init__(path, **kwargs)
        self.document = document

    def target_directory(self) -> Path:
        """Where a new file/folder should be created: the selected folder,
        the parent of the selected file, or the tree root if nothing is
        selected."""
        node = self.cursor_node
        if node is None or node.data is None:
            return Path(self.path)
        path = node.data.path
        return path if path.is_dir() else path.parent

    def action_new_file(self) -> None:
        self.app.begin_create_entry(self.target_directory(), is_dir=False)

    def action_new_directory(self) -> None:
        self.app.begin_create_entry(self.target_directory(), is_dir=True)

    def render_label(self, node, base_style, style) -> Text:
        label = super().render_label(node, base_style, style)
        path = node.data.path if node.data else None
        if path is not None and self.document.has_unsaved_under(path):
            label = Text.assemble(label, (" ●", "bold yellow"))
        return label


class DeletePathSuggester(Suggester):
    """Tab-completes the path argument of /delete and /delete_folder."""

    def __init__(self, app: "LayoutApp") -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self._layout_app = app

    async def get_suggestion(self, value: str) -> str | None:
        if value.startswith("/delete_folder "):
            command, dirs_only = "/delete_folder ", True
        elif value.startswith("/delete "):
            command, dirs_only = "/delete ", False
        else:
            return None

        partial = value[len(command) :]
        if not partial or " " in partial:
            # Nothing typed yet, or the path is finished and a flag (e.g.
            # -f) is being typed - nothing sensible left to complete.
            return None

        dir_part, _, name_prefix = partial.rpartition("/")
        if dir_part.startswith("~"):
            base = Path(dir_part).expanduser()
        elif dir_part.startswith("/"):
            base = Path(dir_part)
        else:
            base = self._layout_app.root_path / dir_part

        try:
            entries = sorted(base.iterdir())
        except OSError:
            return None

        for entry in entries:
            # /delete_folder only ever wants a directory. /delete ultimately
            # deletes a file, but folders still need to suggest so you can
            # Tab into them on the way to one - only the trailing "/" (not
            # the command) depends on what the matched entry actually is.
            if dirs_only and not entry.is_dir():
                continue
            if entry.name.startswith(name_prefix):
                completed = f"{dir_part}/{entry.name}" if dir_part else entry.name
                if entry.is_dir():
                    completed += "/"
                return f"{command}{completed}"
        return None


class CommandInput(Input):
    """Input that accepts a suggestion with Tab instead of the default
    Right/End, and forwards Up/Down to the live /search results list
    (Input itself doesn't use those keys for anything)."""

    BINDINGS = [
        Binding("tab", "accept_suggestion", "Complete", show=False),
        Binding("up", "search_move(-1)", "Previous result", show=False),
        Binding("down", "search_move(1)", "Next result", show=False),
    ]

    def action_accept_suggestion(self) -> None:
        if self._suggestion:
            self.value = self._suggestion
            self.cursor_position = len(self.value)

    def action_search_move(self, delta: int) -> None:
        self.app.move_search_selection(delta)


class SearchResultItem(ListItem):
    def __init__(self, path: Path, label: str) -> None:
        super().__init__(Label(label))
        self.path = path


class SearchResults(ListView):
    """Live /search results shown in the bottom panel."""

    def set_results(self, paths: list[Path], root: Path) -> None:
        self.clear()
        for path in paths:
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            self.append(SearchResultItem(path, rel))
        if paths:
            self.index = 0

    def move_selection(self, delta: int) -> None:
        if delta > 0:
            self.action_cursor_down()
        else:
            self.action_cursor_up()

    @property
    def selected_path(self) -> Path | None:
        child = self.highlighted_child
        return child.path if isinstance(child, SearchResultItem) else None
