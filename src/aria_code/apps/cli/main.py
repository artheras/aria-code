"""Installed console entrypoint for Aria Code.

The terminal implementation lives in ``aria_cli`` as an async ``main()``. A
console script (declared in pyproject ``[project.scripts]`` as
``aria-code = "apps.cli.main:main"``) must be a *synchronous* callable, so this
shim wraps the coroutine in ``asyncio.run`` — mirroring the
``if __name__ == "__main__"`` block in ``aria_cli.py``. Calling the async
``aria_cli.main`` directly from a console script would only create a coroutine
that is never awaited.
"""

from __future__ import annotations

import asyncio
import sys


def main() -> None:
    """Synchronous entry point for the ``aria-code`` command."""
    from aria_code.apps.cli.native_entry import dispatch_worker, maybe_native
    dispatch_worker()
    maybe_native()
    if sys.argv[1:2] == ["review"]:
        # Headless review for CI: no REPL, no banner, an exit code to gate on.
        # Dispatched before the full CLI loads, so it starts quickly.
        from aria_code.apps.cli.review_runner import run_headless

        sys.exit(run_headless(sys.argv[2:]))

    from aria_cli import main as _async_main

    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        print()
        sys.exit(0)


__all__ = ["main"]
