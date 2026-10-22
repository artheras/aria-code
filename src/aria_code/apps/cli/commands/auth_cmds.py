"""Account commands use the same Firebase UID as Web and desktop."""

from __future__ import annotations
import asyncio
import sys
from datetime import datetime, timezone
from ._ui import print_error


def _esc_watcher():
    from aria_code.aria_cli import _esc_watcher as watcher

    return watcher


class AuthCommandsMixin:
    """Mixin: authentication commands (/login, /logout, /whoami)."""

    async def cmd_login(self, args: str):
        """Sign in to Arthera.

        Usage: /login                    — sign in or sign up in the browser
               /login <email>            — email + password, prompted securely

        Bare ``/login`` opens the browser, the way Claude Code and Codex do it.
        Signing in there covers signing up too: the gateway serves the model, so
        a new account has a working setup without configuring any API key. The
        email/password path stays for accounts that predate it and for machines
        with no browser.
        """
        import getpass as _getpass

        parts = args.split()

        if not parts or parts[0].lower() in {"google", "--google", "-g", "web", "--web"}:
            await self._login_with_google()
            return

        if parts[0].lower() in {"email", "--email", "password", "--password"}:
            parts = parts[1:]

        if parts:
            email = parts[0]
        else:
            try:
                prompt_fn = self.context.console.input if self.context.has_rich else input
                email = prompt_fn("  Email: ").strip()
            except (EOFError, KeyboardInterrupt):
                self.context.console.print("[dim]Cancelled[/dim]" if self.context.has_rich else "Cancelled")
                return
        if not email:
            self.context.console.print(
                "[dim]Usage: /login <email>[/dim]" if self.context.has_rich else "Usage: /login <email>"
            )
            return

        # Always prompt for password — never accept it as a CLI argument (security)
        try:
            _esc_watcher().pause()
            password = _getpass.getpass("  Password: ")
        except (EOFError, KeyboardInterrupt):
            self.context.console.print("[dim]Cancelled[/dim]" if self.context.has_rich else "Cancelled")
            return
        finally:
            _esc_watcher().resume()

        if not password:
            self.context.console.print(
                "[red]Password cannot be empty[/red]" if self.context.has_rich else "Password cannot be empty"
            )
            return

        if self.context.has_rich:
            self.context.console.print("[dim]Authenticating...[/dim]")

        try:
            from aria_code.account_session import password_session

            session = await asyncio.to_thread(password_session, email.strip(), password)
            self._accept_session(session)
            who = self.terminal.config.get("email") or session["user_id"]
            self.context.console.print(f"Signed in as {who}")
        except Exception:
            print_error(
                self.context, "Sign-in failed. Check your email/password or run /login in the browser.", "login"
            )

    async def _login_with_google(self):
        """Browser-based account sign-in (see apps/cli/web_login.py).

        Run off the event loop: the flow blocks on a loopback socket for as long
        as the user takes in the browser, which would otherwise stall every other
        coroutine in the REPL.
        """
        import asyncio as _asyncio

        from ..google_login import login_api_url

        from ..web_login import run_web_login

        rich = self.context.has_rich
        api_url = login_api_url(self.terminal.api_url)
        self.context.console.print(
            "[dim]Opening your browser to sign in or create an account…[/dim]"
            if rich
            else "Opening your browser to sign in or create an account…"
        )

        try:
            session = await _asyncio.to_thread(run_web_login, api_url)
        except RuntimeError as exc:
            print_error(self.context, str(exc), "login")
            return
        except Exception as exc:
            print_error(self.context, f"Account sign-in failed: {exc}", "login")
            return

        try:
            self._accept_session(session)
        except ValueError:
            print_error(self.context, "The session identity could not be verified. Run /login again.", "login")
            return
        who = self.terminal.config.get("email") or session["user_id"]
        self.context.console.print(f"[green]✓ Signed in as {who}[/green]" if rich else f"Signed in as {who}")

    def _reset_account_context(self):
        from aria_code.memory_manager import MemoryManager

        self.terminal.conversation = []
        self.terminal._last_response = ""
        self.terminal.memory_mgr = MemoryManager(config=self.terminal.config)
        # Personal profile context is resolved per turn, never retained from A.
        from aria_code.apps.cli.helpers import _load_project_context

        for module_name in ("aria_cli", "aria_code.aria_cli"):
            module = sys.modules.get(module_name)
            if module is not None:
                module._PROJECT_CONTEXT = _load_project_context()

    def _accept_session(self, session):
        from aria_code.account_session import claims_for

        token, uid = session.get("auth_token"), session.get("user_id")
        claims = claims_for(token or "")
        if not token or not uid or claims.get("sub") != uid:
            raise ValueError("Missing account identity")
        cfg = self.terminal.config
        cfg.update(
            auth_token=token,
            user_id=uid,
            email=str(claims.get("email") or session.get("email") or "").strip().lower(),
            display_name=session.get("display_name") or claims.get("name") or "",
            auth_provider=session.get("provider") or "firebase",
        )
        cfg.pop("refresh_token", None)
        cfg.pop("token_expires_at", None)
        if session.get("refresh_token"):
            cfg["refresh_token"] = session["refresh_token"]
        if claims.get("exp"):
            cfg["token_expires_at"] = datetime.fromtimestamp(float(claims["exp"]), timezone.utc).isoformat()
        self.context.save_config(cfg)
        self._reset_account_context()

    def cmd_logout(self, args: str):
        self.terminal.config["auth_token"] = None
        self.terminal.config["user_id"] = None
        self.terminal.config.pop("token_expires_at", None)
        # Added by the Google flow; leaving them behind would let /whoami report
        # a provider for a session that no longer exists.
        self.terminal.config.pop("refresh_token", None)
        self.terminal.config.pop("auth_provider", None)
        self.terminal.config.pop("email", None)
        self.terminal.config.pop("display_name", None)
        self.context.save_config(self.terminal.config)
        self._reset_account_context()
        self.context.console.print("[dim]Logged out[/dim]" if self.context.has_rich else "Logged out")

    def cmd_whoami(self, args: str):
        """Show current authentication status."""
        cfg = self.terminal.config
        user_id = cfg.get("user_id")
        token = cfg.get("auth_token")
        email = cfg.get("email") or ""
        expires = cfg.get("token_expires_at")

        if not token:
            self.context.console.print(
                "[dim]Not logged in — use /login <email>[/dim]" if self.context.has_rich else "Not logged in"
            )
            return

        if self.context.has_rich:
            self.context.console.print()
            self.context.console.print(f"  [dim]User:[/dim]    {user_id or 'unknown'}")
            self.context.console.print(f"  [dim]Email:[/dim]   {email or 'unknown'}")
            if expires:
                # Check expiry
                try:
                    exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                    now = datetime.now(exp_dt.tzinfo)
                    if now > exp_dt:
                        self.context.console.print(f"  [dim]Expires:[/dim] [red]EXPIRED ({expires[:10]})[/red]")
                        self.context.console.print("  [dim]Run /login to refresh your session[/dim]")
                    else:
                        delta = exp_dt - now
                        hours = int(delta.total_seconds() // 3600)
                        self.context.console.print(f"  [dim]Expires:[/dim] {expires[:10]} [dim](in {hours}h)[/dim]")
                except Exception:
                    self.context.console.print(f"  [dim]Expires:[/dim] {expires}")
            self.context.console.print()
        else:
            print(f"User: {user_id or 'unknown'}")
            print(f"Email: {email or 'unknown'}")
            if expires:
                print(f"Expires: {expires}")
