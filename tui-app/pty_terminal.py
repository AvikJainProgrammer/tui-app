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


_HEX_DIGITS = set("0123456789abcdefABCDEF")
_PYTE_COLOR_FIXUPS = {"brown": "yellow"}


def _pyte_color(value: str | None) -> str | None:
    """Convert a pyte Char.fg/bg value to a color Rich will accept.

    pyte represents 256-color/truecolor as a bare 6-digit hex string
    (Rich requires a leading "#"), and uses "brown"/"brightXXX" names that
    don't exist in Rich's palette (Rich wants "yellow"/"bright_xxx"). Any
    of these left unconverted raises inside Style(), which previously
    crashed mid-paint and froze the whole panel with no further updates.
    """
    if value in (None, "default"):
        return None
    if len(value) == 6 and all(c in _HEX_DIGITS for c in value):
        return f"#{value}"
    if value.startswith("bright") and not value.startswith("bright_"):
        value = "bright_" + value[len("bright") :]
    return _PYTE_COLOR_FIXUPS.get(value, value)


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
        # This widget mounts hidden (it's the non-initial child of a
        # ContentSwitcher), so self.size is genuinely (0, 0) here - fall
        # back to a plausible default rather than a degenerate 1x1 pty,
        # which is enough to send bash's readline into a bad state until
        # the first real resize (see on_resize).
        cols = self.size.width or 80
        rows = self.size.height or 24
        self._start_shell(rows, cols)

    def on_unmount(self) -> None:
        self._stop_shell()

    def on_resize(self, event: events.Resize) -> None:
        cols = event.size.width or 80
        rows = event.size.height or 24
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
            return
        # The ioctl alone doesn't notify an already-running shell (or
        # whatever it's currently running in the foreground, e.g. vim) that
        # the size changed - without SIGWINCH, bash's readline in particular
        # keeps assuming its old (possibly 1x1-at-startup) size and can
        # misbehave badly, which looks just like a hang.
        try:
            pgrp = os.tcgetpgrp(self._fd)
            os.killpg(pgrp, signal.SIGWINCH)
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
            try:
                os.write(self._fd, data)
            except OSError:
                pass

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
        try:
            return Style(
                color=_pyte_color(char.fg),
                bgcolor=_pyte_color(char.bg),
                bold=char.bold or None,
                italic=char.italics or None,
                underline=char.underscore or None,
                reverse=char.reverse or None,
                strike=char.strikethrough or None,
            )
        except Exception:
            # Never let an unrecognized pyte color value break rendering -
            # that previously froze the whole panel (see _pyte_color).
            return Style()
