from __future__ import annotations

import shutil
import sys
from enum import Enum
from pathlib import Path
from typing import Callable

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import (
    ContentSwitcher,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Static,
    TextArea,
)

from document import Document
from files import FileKind, load_text_file
from pty_terminal import PtyTerminal
from search_index import build_index, load_index, query_index, save_index
from widgets import CommandInput, DeletePathSuggester, FileTree, RightPanel, SearchResults

# Files larger than this are shown read-only rather than loaded into the
# editor, so a save can never silently truncate a file we didn't fully load.
MAX_EDIT_BYTES = 2_000_000

# Textual's TextArea only ships tree-sitter grammars for these languages.
EXTENSION_LANGUAGES = {
    ".py": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".md": "markdown",
    ".markdown": "markdown",
    ".sh": "bash",
    ".bash": "bash",
    ".sql": "sql",
    ".toml": "toml",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}

COMMANDS = {
    "tree": "Browse files in the left panel (optionally: /tree <path>); select a file to edit it (Ctrl+S to save), n/N to create a file/folder",
    "left": "Reset the left panel to its placeholder",
    "term": "Show an interactive terminal in the bottom panel (F2 to leave it)",
    "bottom": "Reset the bottom panel to its placeholder",
    "delete": "Delete a file (Tab to autocomplete the path)",
    "delete_folder": "Delete an empty folder, or add -f/--force to delete it and everything inside",
    "index": "Index all files under the root so /search works (saved to disk, persists across restarts)",
    "search": "Type a keyword after /search for live results in the bottom panel (Up/Down to pick, Enter to open)",
    "help": "List available commands",
}


def _detect_language(path: Path) -> str | None:
    return EXTENSION_LANGUAGES.get(path.suffix.lower())


class InputMode(Enum):
    """What the command bar is currently being used for."""

    COMMAND = "command"
    CREATE_FILE = "create_file"
    CREATE_DIR = "create_dir"
    SEARCH = "search"


class LayoutApp(App):
    CSS_PATH = "app.tcss"
    # Nothing should grab focus just because it's the first focusable widget
    # in the DOM - without this, Textual's default auto-focus means typing
    # (or even just resizing the terminal) can silently dump keystrokes into
    # the editor with no widget deliberately focused by the user.
    AUTO_FOCUS = None
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("slash", "open_command_bar", "Command"),
        # priority=True: TextArea's own tab_behavior="indent" mode hijacks a
        # plain Escape to mean "focus_next" and consumes it before it would
        # ever bubble up to a normal binding, so this must be checked before
        # the key reaches the focused widget at all. check_action() below
        # still lets it fall through untouched to the terminal, for vim.
        Binding("escape", "dismiss_overlay", "Cancel", priority=True),
        ("f2", "detach_terminal", "Leave Terminal"),
        ("ctrl+s", "save_file", "Save"),
    ]

    def __init__(self, root_path: Path | None = None) -> None:
        super().__init__()
        self.root_path = root_path or Path.cwd()
        self.document = Document()
        self._input_mode = InputMode.COMMAND
        self._create_target_dir: Path | None = None
        # What the bottom panel showed before a live search took it over,
        # so it can be restored once the search ends.
        self._bottom_before_search: str | None = None
        # Loaded from disk if /index was run in an earlier session.
        self.search_index: dict | None = load_index(self.root_path)

        self._commands: dict[str, Callable[[str], None]] = {
            "tree": self._cmd_tree,
            "left": self._cmd_left,
            "term": self._cmd_term,
            "bottom": self._cmd_bottom,
            "delete": self._cmd_delete,
            "delete_folder": self._cmd_delete_folder,
            "index": self._cmd_index,
            "search": self._cmd_search,
            "help": self._cmd_help,
        }

    # -- Frequently-used widgets, queried by ID everywhere else ------------

    @property
    def workspace(self) -> TextArea:
        return self.query_one("#workspace", TextArea)

    @property
    def file_tree(self) -> FileTree:
        return self.query_one("#left-tree", FileTree)

    @property
    def command_bar(self) -> CommandInput:
        return self.query_one("#command-bar", CommandInput)

    @property
    def terminal(self) -> PtyTerminal:
        return self.query_one("#bottom-term", PtyTerminal)

    @property
    def search_results(self) -> SearchResults:
        return self.query_one("#bottom-search", SearchResults)

    @property
    def left_switcher(self) -> ContentSwitcher:
        return self.query_one("#left", ContentSwitcher)

    @property
    def bottom_switcher(self) -> ContentSwitcher:
        return self.query_one("#bottom", ContentSwitcher)

    # -- App setup -----------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "dismiss_overlay" and isinstance(self.focused, PtyTerminal):
            # Let the priority Escape binding fall through to the terminal
            # untouched instead of dismissing anything, since vim (etc.)
            # needs a real, single Escape.
            return False
        return True

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="body"):
            with ContentSwitcher(initial="left-placeholder", id="left"):
                yield Static("Left Panel", id="left-placeholder")
                yield FileTree(str(self.root_path), self.document, id="left-tree")
            yield TextArea(
                id="workspace",
                theme="vscode_dark",
                show_line_numbers=True,
                tab_behavior="indent",
                placeholder="Use /tree to browse files, then select one to open it here.",
            )
            yield RightPanel("Right Panel", id="right")
        with ContentSwitcher(initial="bottom-placeholder", id="bottom"):
            yield Static("Bottom Panel", id="bottom-placeholder")
            yield PtyTerminal(id="bottom-term")
            yield SearchResults(id="bottom-search")
        yield CommandInput(
            id="command-bar",
            select_on_focus=False,
            suggester=DeletePathSuggester(self),
        )
        yield Footer()

    def on_mount(self) -> None:
        self.command_bar.display = False
        self.set_focus(None)

    # -- Global key bindings ---------------------------------------------

    def action_open_command_bar(self) -> None:
        bar = self.command_bar
        bar.display = True
        bar.value = "/"
        bar.cursor_position = len(bar.value)
        bar.focus()

    def action_dismiss_overlay(self) -> None:
        # Escape always blurs whatever is focused (editor, tree, ...), not
        # just the command bar - that's what lets "/" work again afterwards
        # without reaching for the mouse, since a focused widget otherwise
        # swallows the keystroke as ordinary input instead of a binding.
        if self.command_bar.display:
            self._close_command_bar()
        else:
            self.set_focus(None)

    def action_detach_terminal(self) -> None:
        # F2 is intercepted by PtyTerminal itself (rather than forwarded to
        # the shell) specifically so it can bubble here and blur it; Escape
        # is deliberately left alone since shell programs like vim need it.
        if isinstance(self.focused, PtyTerminal):
            self.set_focus(None)

    def action_save_file(self) -> None:
        path = self.document.open_path
        if path is None or self.workspace.read_only:
            return
        text = self.workspace.text
        try:
            path.write_text(text)
        except OSError as e:
            self.notify(f"Could not save {path}:\n{e}", severity="error")
            return
        self.document.mark_saved(path, text)
        self._refresh_tree_markers()
        self.notify(f"Saved {path}", timeout=2)

    def _refresh_tree_markers(self) -> None:
        # Tree caches rendered lines internally, so a plain refresh() would
        # just repaint that cache unchanged - render_label() (where the
        # unsaved-change marker is added) only gets re-run for a node once
        # its cache entry is invalidated, which is what this actually does.
        self.file_tree._invalidate()

    # -- Editor / file-open plumbing --------------------------------------

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        path = self.document.open_path
        if path is None or event.text_area.read_only:
            return
        if self.document.record_change(path, event.text_area.text):
            self._refresh_tree_markers()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self._open_file_in_workspace(event.path)

    def _open_file_in_workspace(self, path: Path) -> None:
        workspace = self.workspace

        buffered = self.document.buffered_text(path)
        if buffered is not None:
            # Unsaved edits from earlier in this session - restore them
            # rather than re-reading the (older) content on disk.
            workspace.read_only = False
            workspace.language = _detect_language(path)
            workspace.load_text(buffered)
            self.document.open(path)
            return

        loaded = load_text_file(path, MAX_EDIT_BYTES)

        if loaded.kind is FileKind.UNREADABLE:
            workspace.read_only = True
            workspace.language = None
            workspace.load_text(f"Could not read {path}:\n{loaded.error}")
            self.document.close_open()
            return

        if loaded.kind is FileKind.BINARY:
            workspace.read_only = True
            workspace.language = None
            workspace.load_text(f"{path}\n\n(binary file, not shown)")
            self.document.close_open()
            return

        if loaded.kind is FileKind.TOO_LARGE:
            text = loaded.text + f"\n\n... truncated ({loaded.size:,} bytes total, too large to edit)"
            workspace.read_only = True
            workspace.language = _detect_language(path)
            workspace.load_text(text)
            self.document.close_open()
            self.notify(
                f"{path.name} is too large to edit ({loaded.size:,} bytes) - showing read-only preview.",
                severity="warning",
            )
            return

        workspace.read_only = False
        workspace.language = _detect_language(path)
        self.document.remember_saved(path, loaded.text)
        workspace.load_text(loaded.text)
        self.document.open(path)

    # -- Command bar: submit / live-typing routing ------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "command-bar":
            return
        raw = event.value
        mode = self._input_mode
        selected_path = self.search_results.selected_path if mode is InputMode.SEARCH else None
        self._close_command_bar()

        if mode is InputMode.SEARCH:
            if selected_path is not None:
                self._open_file_in_workspace(selected_path)
                # Unlike tree selection (which leaves the tree focused so
                # you can keep browsing), picking a search result is a
                # one-shot action - focus the editor immediately so
                # scrolling (arrows, PageUp/Down) works without an extra
                # step, whether or not the file ends up editable.
                self.workspace.focus()
            return
        if mode is InputMode.COMMAND:
            self.run_command(raw)
        else:
            self._create_entry(raw, is_dir=mode is InputMode.CREATE_DIR)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "command-bar" or self._input_mode not in (
            InputMode.COMMAND,
            InputMode.SEARCH,
        ):
            return
        value = event.value
        # Backspacing the bar fully empty cancels it (but ignore the value
        # reset we do ourselves after a submit, which races with any focus
        # change the executed command makes).
        if value == "" and event.input.display:
            self._close_command_bar()
            return
        if value.startswith("/search "):
            self._update_search(value[len("/search ") :])
        elif self._input_mode is InputMode.SEARCH:
            self._exit_search_mode()

    def _close_command_bar(self) -> None:
        bar = self.command_bar
        bar.value = ""
        bar.display = False
        bar.placeholder = ""
        # Must run before resetting _input_mode below: _exit_search_mode()
        # only restores the bottom panel if mode is still SEARCH.
        self._exit_search_mode()
        self._input_mode = InputMode.COMMAND
        self.set_focus(None)

    # -- Live /search -----------------------------------------------------

    def _enter_search_mode(self) -> None:
        self._input_mode = InputMode.SEARCH
        self._bottom_before_search = self.bottom_switcher.current
        self.bottom_switcher.current = "bottom-search"

    def _exit_search_mode(self) -> None:
        if self._input_mode is not InputMode.SEARCH:
            return
        self._input_mode = InputMode.COMMAND
        self.bottom_switcher.current = self._bottom_before_search or "bottom-placeholder"
        self._bottom_before_search = None

    def _update_search(self, query: str) -> None:
        if self._input_mode is not InputMode.SEARCH:
            self._enter_search_mode()
        if self.search_index is None:
            self.search_results.set_results([], self.root_path)
            return
        matches = query_index(self.search_index, query)
        self.search_results.set_results([self.root_path / m for m in matches], self.root_path)

    def move_search_selection(self, delta: int) -> None:
        if self._input_mode is InputMode.SEARCH:
            self.search_results.move_selection(delta)

    # -- New file/folder (n / N in the tree) -------------------------------

    def begin_create_entry(self, directory: Path, is_dir: bool) -> None:
        self._create_target_dir = directory
        self._input_mode = InputMode.CREATE_DIR if is_dir else InputMode.CREATE_FILE
        bar = self.command_bar
        kind = "folder" if is_dir else "file"
        bar.placeholder = f"New {kind} name in {directory}/"
        bar.value = ""
        bar.display = True
        bar.focus()

    def _create_entry(self, raw_name: str, is_dir: bool) -> None:
        name = raw_name.strip()
        if not name or self._create_target_dir is None:
            return
        target = self._create_target_dir / name
        kind = "folder" if is_dir else "file"
        try:
            if is_dir:
                target.mkdir(parents=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch(exist_ok=False)
        except OSError as e:
            self.notify(f"Could not create {kind} {target}:\n{e}", severity="error")
            return

        self.file_tree.reload()
        self.notify(f"Created {kind} {target}", timeout=2)

        if not is_dir:
            workspace = self.workspace
            workspace.read_only = False
            workspace.language = _detect_language(target)
            self.document.remember_saved(target, "")
            workspace.load_text("")
            self.document.open(target)
            workspace.focus()

    # -- /delete and /delete_folder ----------------------------------------

    def _resolve_within_root(self, path_str: str) -> Path | None:
        """Resolve a /delete[_folder] path argument, kept scoped inside the
        tree's root - typos or accidents shouldn't be able to reach outside
        the directory being browsed."""
        path_str = path_str.strip()
        if not path_str:
            return None
        raw = Path(path_str).expanduser()
        candidate = raw if raw.is_absolute() else self.root_path / raw
        try:
            resolved = candidate.resolve()
            root_resolved = self.root_path.resolve()
        except OSError:
            return None
        if resolved != root_resolved and root_resolved not in resolved.parents:
            return None
        return resolved

    def _forget_deleted(self, path: Path, recursive: bool = False) -> None:
        if not self.document.forget(path, recursive=recursive):
            return
        workspace = self.workspace
        workspace.read_only = True
        workspace.language = None
        workspace.load_text(f"{path} was deleted.")

    def _cmd_delete(self, arg: str) -> None:
        path = self._resolve_within_root(arg)
        if path is None:
            self.notify("Usage: /delete <file>", severity="error")
            return
        if not path.exists():
            self.notify(f"No such file: {path}", severity="error")
            return
        if path.is_dir():
            self.notify(f"{path} is a folder - use /delete_folder", severity="error")
            return
        try:
            path.unlink()
        except OSError as e:
            self.notify(f"Could not delete {path}:\n{e}", severity="error")
            return
        self._forget_deleted(path)
        self.file_tree.reload()
        self.notify(f"Deleted {path}", timeout=2)

    def _cmd_delete_folder(self, arg: str) -> None:
        tokens = arg.split()
        force = False
        path_tokens = []
        for token in tokens:
            if token in ("-f", "--force"):
                force = True
            else:
                path_tokens.append(token)

        path = self._resolve_within_root(" ".join(path_tokens))
        if path is None:
            self.notify("Usage: /delete_folder <folder> [-f|--force]", severity="error")
            return
        if not path.exists():
            self.notify(f"No such folder: {path}", severity="error")
            return
        if not path.is_dir():
            self.notify(f"{path} is a file - use /delete", severity="error")
            return
        if path == self.root_path.resolve():
            self.notify("Cannot delete the tree's root folder", severity="error")
            return

        has_contents = any(path.iterdir())
        if has_contents and not force:
            self.notify(
                f"{path} is not empty - add -f to delete it and everything inside",
                severity="error",
            )
            return

        try:
            if has_contents:
                shutil.rmtree(path)
            else:
                path.rmdir()
        except OSError as e:
            self.notify(f"Could not delete {path}:\n{e}", severity="error")
            return
        self._forget_deleted(path, recursive=True)
        self.file_tree.reload()
        self.notify(f"Deleted folder {path}", timeout=2)

    # -- /index -------------------------------------------------------------

    def _cmd_index(self, arg: str) -> None:
        self.notify(f"Indexing {self.root_path}...", timeout=2)
        self._build_index_worker(self.root_path)

    @work(thread=True, exclusive=True)
    def _build_index_worker(self, root: Path) -> None:
        index = build_index(root)
        try:
            save_index(root, index)
        except OSError as e:
            self.call_from_thread(
                self.notify, f"Could not write index file: {e}", severity="error"
            )
            return
        if root == self.root_path:
            self.search_index = index
        self.call_from_thread(
            self.notify, f"Indexed {len(index['files'])} files in {root}", timeout=3
        )

    # -- Remaining slash commands -------------------------------------------

    def _cmd_tree(self, arg: str) -> None:
        if arg:
            path = Path(arg).expanduser()
            if not path.is_dir():
                self.notify(f"Not a directory: {path}", severity="error")
                return
            self.root_path = path
            self.file_tree.path = str(self.root_path)
            self.search_index = load_index(self.root_path)
        self.left_switcher.current = "left-tree"
        self.file_tree.focus()

    def _cmd_left(self, arg: str) -> None:
        self.left_switcher.current = "left-placeholder"

    def _cmd_term(self, arg: str) -> None:
        self.bottom_switcher.current = "bottom-term"
        self.terminal.focus()
        self.notify("Terminal focused. Press F2 to leave it.", timeout=3)

    def _cmd_bottom(self, arg: str) -> None:
        self.bottom_switcher.current = "bottom-placeholder"

    def _cmd_search(self, arg: str) -> None:
        self.notify("Type a keyword after /search to see live results.", timeout=2)

    def _cmd_help(self, arg: str) -> None:
        self.notify("\n".join(f"/{cmd} - {desc}" for cmd, desc in COMMANDS.items()))

    def run_command(self, raw: str) -> None:
        text = raw.strip()
        if text.startswith("/"):
            text = text[1:]
        if not text:
            return

        name, _, arg = text.partition(" ")
        handler = self._commands.get(name.lower())
        if handler is None:
            self.notify(f"Unknown command: /{name}  (try /help)", severity="error")
            return
        handler(arg.strip())


if __name__ == "__main__":
    arg_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    LayoutApp(root_path=arg_path).run()
