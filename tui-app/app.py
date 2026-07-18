from __future__ import annotations

import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import (
    ContentSwitcher,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Static,
)

from pty_terminal import PtyTerminal

COMMANDS = {
    "tree": "Show a file tree in the left panel (optionally: /tree <path>)",
    "left": "Reset the left panel to its placeholder",
    "term": "Show an interactive terminal in the bottom panel (Escape twice to leave it)",
    "bottom": "Reset the bottom panel to its placeholder",
    "help": "List available commands",
}


class RightPanel(Static):
    """Right sidebar panel."""


class Workspace(Static):
    """Central workspace panel."""


class LayoutApp(App):
    CSS_PATH = "app.tcss"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("slash", "open_command_bar", "Command"),
        ("escape", "dismiss_overlay", "Cancel"),
    ]

    def __init__(self, root_path: Path | None = None) -> None:
        super().__init__()
        self.root_path = root_path or Path.cwd()

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="body"):
            with ContentSwitcher(initial="left-placeholder", id="left"):
                yield Static("Left Panel", id="left-placeholder")
                yield DirectoryTree(str(self.root_path), id="left-tree")
            yield Workspace("Central Workspace", id="workspace")
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
        bar = self.query_one("#command-bar", Input)
        if bar.display:
            bar.value = ""
            bar.display = False
            self.set_focus(None)
        elif isinstance(self.focused, PtyTerminal):
            # Reached only via double-Escape inside the terminal (a single
            # Escape there is forwarded to the shell instead, e.g. for vim).
            self.set_focus(None)

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
        elif name == "left":
            left_switcher.current = "left-placeholder"
        elif name == "term":
            bottom_switcher.current = "bottom-term"
            self.query_one("#bottom-term", PtyTerminal).focus()
        elif name == "bottom":
            bottom_switcher.current = "bottom-placeholder"
        elif name == "help":
            self.notify("\n".join(f"/{cmd} - {desc}" for cmd, desc in COMMANDS.items()))
        else:
            self.notify(f"Unknown command: /{name}  (try /help)", severity="error")


if __name__ == "__main__":
    arg_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    LayoutApp(root_path=arg_path).run()
