from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Header, Footer, Static


class LeftPanel(Static):
    """Left sidebar panel."""


class RightPanel(Static):
    """Right sidebar panel."""


class BottomPanel(Static):
    """Bottom panel."""


class Workspace(Static):
    """Central workspace panel."""


class LayoutApp(App):
    CSS_PATH = "app.tcss"
    BINDINGS = [("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="body"):
            yield LeftPanel("Left Panel", id="left")
            yield Workspace("Central Workspace", id="workspace")
            yield RightPanel("Right Panel", id="right")
        yield BottomPanel("Bottom Panel", id="bottom")
        yield Footer()


if __name__ == "__main__":
    LayoutApp().run()
