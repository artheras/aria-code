"""Arrow-key selector and async wrapper used by model/skill picker dialogs.

    from ui.picker import arrow_select, run_picker_in_thread
"""

from __future__ import annotations

import asyncio
import os
import sys

from aria_code.ui.console import _HAS_TERMIOS, _esc_watcher

if _HAS_TERMIOS:
    import termios
    import tty
    import select as _select


def arrow_select(options: list, selected: int = 0, title: str = "",
                 max_visible: int = 10,
                 controls_hint: str = "↑↓  Enter  Esc/q Cancel",
                 collapse_to: str | None = None,
                 shortcuts: dict | None = None,
                 numbered: bool = False) -> int:
    """Interactive arrow-key selector with scrolling.

    Args:
        options:     list of ``(label, description)`` tuples or plain strings
        selected:    initially highlighted index
        title:       optional header line
        max_visible: max rows shown at once; scrolls when list is larger
        collapse_to: when given, the menu is erased once answered and one
                     line is left in its place — "✓ Yes  fx.py" — instead of
                     the whole menu, as Codex leaves an answered approval.
                     The text names what was being approved.
        shortcuts:   ``{"y": 0, "n": 3}`` — one key answers with that option
                     at once; the key is shown after its label, "(y)".
        numbered:    show "1." … before labels; digits 1–9 answer directly.
    Returns:
        index of chosen option, or -1 if cancelled
    """
    if not options:
        return -1
    keys = {str(key): index for key, index in (shortcuts or {}).items()
            if len(str(key)) == 1 and 0 <= index < len(options)}
    key_of = {index: key for key, index in keys.items()}

    def _label(index: int, label: str) -> str:
        text = f"{index + 1}. {label}" if numbered else label
        return f"{text} ({key_of[index]})" if index in key_of else text

    if not _HAS_TERMIOS or not sys.stdin.isatty():
        if title:
            print(f"\n  {title}\n")
        for i, opt in enumerate(options):
            label = opt[0] if isinstance(opt, tuple) else opt
            marker = "❯" if i == selected else " "
            print(f"  {marker} {i + 1:2d}. {_label(i, label) if keys else label}")
        try:
            c = input("\n  Enter number (or Enter to keep current): ").strip()
            if not c:
                return selected
            if c in keys:
                return keys[c]
            idx = int(c) - 1
            return idx if 0 <= idx < len(options) else -1
        except (ValueError, EOFError, KeyboardInterrupt):
            return -1

    n       = len(options)
    visible = min(max_visible, n)
    scroll  = max(0, selected - visible + 1)

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        _tcols = os.get_terminal_size(fd).columns
    except Exception:
        _tcols = 80

    _rule = "─" * min(_tcols, 72)

    def _display_width(s: str) -> int:
        import re as _re
        clean = _re.sub(r'\x1b\[[0-9;]*[mJKHABCDfGrs]', '', s)
        w = 0
        for ch in clean:
            cp = ord(ch)
            if (0x1100 <= cp <= 0x115F or 0x2E80 <= cp <= 0xA4CF or
                    0xAC00 <= cp <= 0xD7AF or 0xF900 <= cp <= 0xFAFF or
                    0xFE10 <= cp <= 0xFE1F or 0xFE30 <= cp <= 0xFE4F or
                    0xFF01 <= cp <= 0xFF60 or 0xFFE0 <= cp <= 0xFFE6 or
                    0x3000 <= cp <= 0x303F):
                w += 2
            else:
                w += 1
        return w

    def _physical_lines(text: str) -> int:
        dw = _display_width(text)
        return max(1, (dw + _tcols - 1) // _tcols)

    def _raw(s: str):
        os.write(1, s.encode())

    def _render():
        nonlocal scroll
        if selected < scroll:
            scroll = selected
        elif selected >= scroll + visible:
            scroll = selected - visible + 1

        buf = ""
        if _render.drawn:
            buf += f"\033[{_render.last_phys_height}A"

        phys_height = 0
        for row in range(visible):
            idx   = scroll + row
            opt   = options[idx]
            label = opt[0] if isinstance(opt, tuple) else opt
            desc  = opt[1] if isinstance(opt, tuple) and len(opt) > 1 else ""
            if not label.strip().startswith("──"):
                label = _label(idx, label)
            if label.strip().startswith("──"):
                line = f"  \033[2m{label}\033[0m"
            elif idx == selected:
                _accent = os.getenv("ARIA_ACCENT_COLOR", "192;128;80")
                line = f"  \033[1m\033[38;2;{_accent}m❯\033[0m \033[1m{label}\033[0m"
                if desc:
                    line += f"  \033[2m{desc}\033[0m"
            else:
                line = f"    {label}"
                if desc:
                    line += f"  \033[2m{desc}\033[0m"
            buf += f"\033[2K{line}\n"
            phys_height += _physical_lines(line)

        if n > visible:
            hint = f"  \033[2m{selected + 1}/{n}\033[0m"
            buf += f"\033[2K{hint}\n"
        else:
            buf += "\033[2K\n"
        phys_height += 1

        _render.last_phys_height = phys_height
        _raw(buf)
        _render.drawn = True

    _render.drawn = False
    _render.last_phys_height = visible + 1
    header_text = f"  {title}  {controls_hint}" if title else f"  {controls_hint}"
    # blank line + rule, then the title/hint line(s) + blank line
    header_height = 2 + _physical_lines(header_text) + 1
    result = -1

    def _choose() -> int:
        nonlocal selected
        while True:
            ch = os.read(fd, 1)
            if ch == b'\x1b':
                seq = b''
                if _select.select([fd], [], [], 0.05)[0]:
                    seq = os.read(fd, 2)
                if seq == b'[A':
                    selected = (selected - 1) % n; _render()
                elif seq == b'[B':
                    selected = (selected + 1) % n; _render()
                elif seq == b'[5~':
                    selected = max(0, selected - visible); _render()
                elif seq == b'[6~':
                    selected = min(n - 1, selected + visible); _render()
                elif not seq:
                    return -1
            elif ch in (b'\r', b'\n'):
                return selected
            elif ch.decode(errors="ignore") in keys:
                selected = keys[ch.decode(errors="ignore")]
                return selected
            elif numbered and ch.isdigit() and 1 <= int(ch) <= n:
                selected = int(ch) - 1
                return selected
            elif ch == b'q':
                return -1
            elif ch == b'k':
                selected = (selected - 1) % n; _render()
            elif ch == b'j':
                selected = (selected + 1) % n; _render()
            elif ch == b'g':
                selected = 0; _render()
            elif ch == b'G':
                selected = n - 1; _render()
            elif ch in (b'\x03', b'\x04'):
                return -1

    try:
        _esc_watcher.pause()
        tty.setcbreak(fd)
        sys.stdout.flush()

        # Top boundary
        _raw(f"\n  \033[2m{_rule}\033[0m\n")
        if title:
            _raw(f"  \033[1m{title}\033[0m  \033[2m{controls_hint}\033[0m\n\n")
        else:
            _raw(f"  \033[2m{controls_hint}\033[0m\n\n")

        _render()
        result = _choose()
        return result
    except (EOFError, KeyboardInterrupt, OSError):
        return -1
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        _esc_watcher.resume()
        if collapse_to is not None and _render.drawn:
            label = ""
            if 0 <= result < n:
                opt = options[result]
                label = opt[0] if isinstance(opt, tuple) else opt
            declined = result < 0 or label.strip().lower().startswith(("no", "否", "拒绝", "取消", "cancel"))
            mark = "\033[31m✗\033[0m" if declined else "\033[32m✓\033[0m"
            shown = label or "Cancelled"
            subject = f"  \033[2m{collapse_to}\033[0m" if collapse_to else ""
            # Up over the header and the menu, clear to the end, one line instead.
            _raw(f"\033[{header_height + _render.last_phys_height}A\r\033[J  {mark} {shown}{subject}\n")
        else:
            _raw(f"  \033[2m{_rule}\033[0m\n\n")


async def run_picker_in_thread(options: list, current_idx: int,
                               title: str, max_visible: int = 14,
                               controls_hint: str = "↑↓  Enter  Esc/q Cancel") -> int:
    """Run arrow_select in an executor thread to avoid kqueue conflicts on macOS."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: arrow_select(
            options, current_idx, title, max_visible, controls_hint
        ),
    )


# Back-compat aliases used by aria_cli.py
_arrow_select = arrow_select
_run_picker_in_thread = run_picker_in_thread
