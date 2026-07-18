from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import termios
import time

import pyte
from rich.style import Style
from rich.text import Text
from textual import events
from textual.widget import Widget

_reaper_installed = False


def _install_sigchld_reaper() -> None:
    """Reap terminated pty child shells so they don't linger as zombies."""
    global _reaper_installed
    if _reaper_installed:
        return
    _reaper_installed = True

    def _reap(*_args: object) -> None:
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break

    asyncio.get_event_loop().add_signal_handler(signal.SIGCHLD, _reap)


def _terminate_and_reap(pid: int, timeout: float = 0.2) -> None:
    """Terminate a pty child and block briefly until it's reaped, so it
    never lingers as a zombie or outlives the app."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if reaped_pid == pid:
            return
        time.sleep(0.01)

    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass


KEY_BYTES = {
    "enter": b"\r",
    "escape": b"\x1b",
    "tab": b"\t",
    "shift+tab": b"\x1b[Z",
    "backspace": b"\x7f",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "delete": b"\x1b[3~",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "ctrl+a": b"\x01",
    "ctrl+b": b"\x02",
    "ctrl+c": b"\x03",
    "ctrl+d": b"\x04",
    "ctrl+e": b"\x05",
    "ctrl+k": b"\x0b",
    "ctrl+l": b"\x0c",
    "ctrl+r": b"\x12",
    "ctrl+u": b"\x15",
    "ctrl+w": b"\x17",
    "ctrl+z": b"\x1a",
}


class PtyTerminal(Widget, can_focus=True):
    """A widget that runs a real shell in a pty and renders its screen."""

    DEFAULT_CSS = """
    PtyTerminal {
        background: black;
        color: white;
    }
    """

    def __init__(self, command: list[str] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._command = command or [os.environ.get("SHELL", "/bin/bash")]
        self._pid: int | None = None
        self._fd: int | None = None
        self._screen: pyte.Screen | None = None
        self._stream: pyte.Stream | None = None

    def on_mount(self) -> None:
        cols = max(self.size.width, 1) or 80
        rows = max(self.size.height, 1) or 24
        self._start_shell(rows, cols)

    def on_unmount(self) -> None:
        self._stop_shell()

    def on_resize(self, event: events.Resize) -> None:
        cols = max(event.size.width, 1)
        rows = max(event.size.height, 1)
        if self._screen is None:
            self._start_shell(rows, cols)
            return
        self._screen.resize(rows, cols)
        self._set_winsize(rows, cols)
        self.refresh()

    def _start_shell(self, rows: int, cols: int) -> None:
        _install_sigchld_reaper()
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)

        pid, fd = pty.fork()
        if pid == 0:
            env = dict(os.environ)
            env["TERM"] = "xterm-256color"
            os.execvpe(self._command[0], self._command, env)
        self._pid = pid
        self._fd = fd
        self._set_winsize(rows, cols)

        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        asyncio.get_event_loop().add_reader(fd, self._on_readable)

    def _set_winsize(self, rows: int, cols: int) -> None:
        if self._fd is None:
            return
        try:
            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def _on_readable(self) -> None:
        assert self._fd is not None
        try:
            data = os.read(self._fd, 65536)
        except OSError:
            data = b""
        if not data:
            self._stop_shell()
            return
        try:
            self._stream.feed(data.decode(errors="replace"))
        except Exception:
            # pyte can raise on escape sequences it doesn't fully support
            # (e.g. some private CSI...m forms vim emits); it resets its own
            # parser state before re-raising, so just drop this chunk rather
            # than let the exception kill screen updates from here on.
            pass
        self.refresh()

    def _stop_shell(self) -> None:
        if self._fd is not None:
            try:
                asyncio.get_event_loop().remove_reader(self._fd)
            except Exception:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._pid is not None:
            pid = self._pid
            self._pid = None
            _terminate_and_reap(pid)

    async def on_key(self, event: events.Key) -> None:
        if self._fd is None:
            return

        if event.key == "f2":
            # Dedicated detach key: Escape is fully reserved for the shell
            # (vim needs a real, single, un-trapped Escape), so detaching
            # can't reuse it or any other key a shell program might expect.
            return

        event.stop()
        event.prevent_default()
        data = KEY_BYTES.get(event.key)
        if data is None and event.character:
            data = event.character.encode()
        if data:
            os.write(self._fd, data)

    def render(self) -> Text:
        if self._screen is None:
            return Text("")
        text = Text()
        cursor = self._screen.cursor
        lines = self._screen.display
        for y, line in enumerate(lines):
            row = self._screen.buffer.get(y, {})
            for x, ch in enumerate(line):
                char = row.get(x)
                style = self._char_style(char)
                if not cursor.hidden and cursor.x == x and cursor.y == y and self.has_focus:
                    style += Style(reverse=True)
                text.append(ch, style=style)
            if y != len(lines) - 1:
                text.append("\n")
        return text

    @staticmethod
    def _char_style(char: pyte.screens.Char | None) -> Style:
        if char is None:
            return Style()
        fg = None if char.fg in (None, "default") else char.fg
        bg = None if char.bg in (None, "default") else char.bg
        return Style(
            color=fg,
            bgcolor=bg,
            bold=char.bold or None,
            italic=char.italics or None,
            underline=char.underscore or None,
            reverse=char.reverse or None,
            strike=char.strikethrough or None,
        )
