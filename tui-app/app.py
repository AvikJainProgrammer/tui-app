from __future__ import annotations

import sys
from pathlib import Path

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

from pty_terminal import PtyTerminal

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
    "tree": "Browse files in the left panel (optionally: /tree <path>); select a file to edit it in the workspace (Ctrl+S to save)",
    "left": "Reset the left panel to its placeholder",
    "term": "Show an interactive terminal in the bottom panel (F2 to leave it)",
    "bottom": "Reset the bottom panel to its placeholder",
    "help": "List available commands",
}


class RightPanel(Static):
    """Right sidebar panel."""


def _detect_language(path: Path) -> str | None:
    return EXTENSION_LANGUAGES.get(path.suffix.lower())


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
        self.open_file_path: Path | None = None

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
                yield DirectoryTree(str(self.root_path), id="left-tree")
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
        yield Input(id="command-bar", select_on_focus=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#command-bar", Input).display = False
        self.set_focus(None)

    def action_open_command_bar(self) -> None:
        bar = self.query_one("#command-bar", Input)
        bar.display = True
        bar.value = "/"
        bar.cursor_position = len(bar.value)
        bar.focus()

    def action_dismiss_overlay(self) -> None:
        # Escape always blurs whatever is focused (editor, tree, ...), not
        # just the command bar - that's what lets "/" work again afterwards
        # without reaching for the mouse, since a focused widget otherwise
        # swallows the keystroke as ordinary input instead of a binding.
        bar = self.query_one("#command-bar", Input)
        if bar.display:
            bar.value = ""
            bar.display = False
        self.set_focus(None)

    def action_detach_terminal(self) -> None:
        # F2 is intercepted by PtyTerminal itself (rather than forwarded to
        # the shell) specifically so it can bubble here and blur it; Escape
        # is deliberately left alone since shell programs like vim need it.
        if isinstance(self.focused, PtyTerminal):
            self.set_focus(None)

    def action_save_file(self) -> None:
        if self.open_file_path is None:
            return
        workspace = self.query_one("#workspace", TextArea)
        if workspace.read_only:
            return
        try:
            self.open_file_path.write_text(workspace.text)
        except OSError as e:
            self.notify(f"Could not save {self.open_file_path}:\n{e}", severity="error")
            return
        self.notify(f"Saved {self.open_file_path}", timeout=2)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        workspace = self.query_one("#workspace", TextArea)
        path = event.path

        try:
            data = path.read_bytes()
        except OSError as e:
            workspace.read_only = True
            workspace.language = None
            workspace.load_text(f"Could not read {path}:\n{e}")
            self.open_file_path = None
            return

        if b"\x00" in data[:8000]:
            workspace.read_only = True
            workspace.language = None
            workspace.load_text(f"{path}\n\n(binary file, not shown)")
            self.open_file_path = None
            return

        if len(data) > MAX_EDIT_BYTES:
            text = data[:MAX_EDIT_BYTES].decode("utf-8", errors="replace")
            text += f"\n\n... truncated ({len(data):,} bytes total, too large to edit)"
            workspace.read_only = True
            workspace.language = _detect_language(path)
            workspace.load_text(text)
            self.open_file_path = None
            self.notify(
                f"{path.name} is too large to edit ({len(data):,} bytes) - showing read-only preview.",
                severity="warning",
            )
            return

        workspace.read_only = False
        workspace.language = _detect_language(path)
        workspace.load_text(data.decode("utf-8", errors="replace"))
        self.open_file_path = path

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "command-bar":
            return
        raw = event.value
        bar = event.input
        bar.display = False
        bar.value = ""
        self.set_focus(None)
        self.run_command(raw)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "command-bar":
            return
        # Backspacing the bar fully empty cancels it (but ignore the value
        # reset we do ourselves after a submit, which races with any focus
        # change the executed command makes).
        if event.value == "" and event.input.display:
            event.input.display = False
            self.set_focus(None)

    def run_command(self, raw: str) -> None:
        text = raw.strip()
        if text.startswith("/"):
            text = text[1:]
        if not text:
            return

        parts = text.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        left_switcher = self.query_one("#left", ContentSwitcher)
        bottom_switcher = self.query_one("#bottom", ContentSwitcher)

        if name == "tree":
            if arg:
                path = Path(arg).expanduser()
                if not path.is_dir():
                    self.notify(f"Not a directory: {path}", severity="error")
                    return
                self.root_path = path
                self.query_one("#left-tree", DirectoryTree).path = str(self.root_path)
            left_switcher.current = "left-tree"
            self.query_one("#left-tree", DirectoryTree).focus()
        elif name == "left":
            left_switcher.current = "left-placeholder"
        elif name == "term":
            bottom_switcher.current = "bottom-term"
            self.query_one("#bottom-term", PtyTerminal).focus()
            self.notify("Terminal focused. Press F2 to leave it.", timeout=3)
        elif name == "bottom":
            bottom_switcher.current = "bottom-placeholder"
        elif name == "help":
            self.notify("\n".join(f"/{cmd} - {desc}" for cmd, desc in COMMANDS.items()))
        else:
            self.notify(f"Unknown command: /{name}  (try /help)", severity="error")


if __name__ == "__main__":
    arg_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    LayoutApp(root_path=arg_path).run()
