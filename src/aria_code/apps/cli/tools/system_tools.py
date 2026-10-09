"""System-level tools: run_command, web_fetch, github.

All functions are pure (no module-level globals). Console output is
injected via keyword args so aria_cli.py thin wrappers supply the
Rich console and global state defaults.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent.parent  # aria-code/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aria_code.safety import evaluate_command_policy  # noqa: E402


def _persist_command_output(command: str, stdout: str, stderr: str, returncode: int) -> dict:
    """Persist full command output when it is too large for inline tool context."""
    stdout_lines = stdout.splitlines()
    stderr_lines = stderr.splitlines()
    if (
        len(stdout) <= 2500
        and len(stderr) <= 1200
        and len(stdout_lines) <= 18
        and len(stderr_lines) <= 8
    ):
        return {}
    try:
        from aria_code.artifacts import create_artifact, write_artifact_metadata

        record = create_artifact(
            "command-output",
            "shell",
            "command_output",
            ".txt",
            timestamp=datetime.now(),
        )
        text = (
            f"$ {command}\n"
            f"exit_code={returncode}\n\n"
            "===== STDOUT =====\n"
            f"{stdout}"
            "\n\n===== STDERR =====\n"
            f"{stderr}"
        )
        record.path.write_text(text, encoding="utf-8", errors="replace")
        write_artifact_metadata(record, {
            "kind": "command_output",
            "status": "complete" if returncode == 0 else "failed",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "command": command,
            "exit_code": returncode,
            "stdout_chars": len(stdout),
            "stderr_chars": len(stderr),
            "stdout_truncated_inline": len(stdout) > 5000,
            "stderr_truncated_inline": len(stderr) > 2000,
        })
        return {"full_output_path": str(record.path)}
    except Exception:
        return {}


def _cprint(msg: str, *, console, has_rich: bool) -> None:
    if has_rich and console is not None:
        console.print(msg)
    else:
        plain = re.sub(r"\[/?[^\]]+\]", "", msg)
        print(plain)


OUTPUT_PREVIEW_LINES = 5


def print_command_outcome(console, returncode: int, stdout: str, stderr: str, full_output_path: str = "") -> None:
    """One step line and the last few lines of what the command printed.

    This printed "Command completed (exit 0)", up to six stdout lines and
    "full output saved" — and nothing at all for `python3 -m unittest -v`,
    whose report goes to stderr, so the model pasted the test results into
    its answer instead. Lines are plain text: program output such as
    "[/dim]" is not Rich markup. As Codex does, the tail is kept — that is
    where "OK" or the failure is — and the rest is counted.
    """
    from rich.text import Text

    lines = stdout.rstrip().splitlines() + (stderr.rstrip().splitlines() if stderr.strip() else [])
    ok = returncode == 0
    head = Text("  ⎿  ", style="dim")
    head.append(f"exit {returncode}", style="green" if ok else "red")
    if lines:
        head.append(f" · {len(lines)} line{'s' if len(lines) != 1 else ''}", style="dim")
    console.print(head)
    notes = []
    if len(lines) > OUTPUT_PREVIEW_LINES:
        notes.append(f"… +{len(lines) - OUTPUT_PREVIEW_LINES} lines")
    if full_output_path:
        from aria_code.ui.render.output import display_path

        notes.append(f"full output saved: {display_path(full_output_path)}")
    if notes:
        console.print(Text("     " + " · ".join(notes), style="dim"))
    for line in lines[-OUTPUT_PREVIEW_LINES:]:
        console.print(Text("     " + line[:160], style="dim" if ok else "red"), overflow="ellipsis", no_wrap=True)


def tool_run_command(
    params: dict,
    *,
    console=None,
    has_rich: bool = True,
    quiet: bool = False,
) -> dict:
    """Run a shell command and return output.

    ``params`` should contain ``permission_mode`` and ``network_enabled``
    (filled in by the aria_cli.py wrapper from the active globals).
    ``quiet`` leaves the outcome to the caller: during a chat turn the
    transcript's Ran cell shows the exit and the output tail itself.
    """
    command = params.get("command", "")
    # LLMs sometimes send command as a list e.g. ['bash', '-lc', '...'] — normalize to string
    if isinstance(command, list):
        import shlex as _shlex
        command = _shlex.join(str(c) for c in command)
        params["command"] = command
    if not command:
        return {"success": False, "error": "Missing 'command' parameter"}

    effective_policy = params.get("policy", "safe")
    if params.get("user_approved") and effective_policy == "safe":
        effective_policy = "balanced"

    decision = evaluate_command_policy(
        command,
        effective_policy,
        # "safe" is a *policy* name, not a mode — it never matched
        # PermissionMode and silently resolved to workspace-write. Both
        # executors supply a real mode, so this default is only reached
        # by a caller that bypasses them; read-only is the right answer
        # there.
        mode=params.get("permission_mode", "read-only"),
        network_enabled=bool(params.get("network_enabled", True)),
    )
    mode = str(params.get("permission_mode", "read-only"))
    network = bool(params.get("network_enabled", True))
    sandbox_hint = ""
    command = decision.normalized_command

    from aria_code.safety.permissions import writes_a_device
    dangerous = ["rm -rf /", "mkfs", "dd if=", ":(){ :", "fork bomb"]
    if any(d in command for d in dangerous) or writes_a_device(command):
        return {"success": False, "error": f"Blocked dangerous command: {command}"}

    # Prevent executing text/doc files as Python — they are analysis reports, not scripts
    import re as _re_cmd
    _py3_file = _re_cmd.search(r'\bpython3?\s+["\']?(\S+\.(?:txt|md|docx|csv|json|log))', command)
    if _py3_file:
        _bad_file = _py3_file.group(1)
        return {
            "success": False,
            "error": (
                f"拒绝执行: '{_bad_file}' 是文本/分析文件，不是 Python 脚本。\n"
                "如需展示分析结果，请直接输出文字，或将分析结论写入 .py 文件后执行。"
            ),
        }

    if params.get("dry_run"):
        return {"success": True, "data": {
            "command": command,
            "risk": decision.risk,
            "policy": decision.policy,
            "requires_approval": getattr(decision, "requires_approval", False),
            "network": getattr(decision, "network", False),
            "dry_run": True,
        }}
    if not decision.allowed:
        return {"success": False, "error": decision.reason}
    try:
        cwd = params.get("cwd", None)
        timeout = min(params.get("timeout", 120), 300)
        use_shell = True
        argv = None
        # POSIX shlex strips Windows path backslashes. Keep CMD's own parsing
        # on Windows, after the same policy and permission checks above.
        if decision.risk == "low" and os.name != "nt":
            has_shell_meta = any(ch in command for ch in ["|", "&", ";", "<", ">", "$", "`", "\n"])
            if not has_shell_meta:
                try:
                    argv = shlex.split(command)
                    if argv:
                        use_shell = False
                except ValueError:
                    use_shell = True
                    argv = None

        sandbox = params.get("sandbox", False)
        if params.get("background"):
            return _start_background(params, command, argv, use_shell, cwd, mode, network,
                                     docker=bool(sandbox), console=console, has_rich=has_rich)
        if sandbox:
            try:
                from aria_code.apps.cli.sandbox import run_in_docker_sandbox
                result = run_in_docker_sandbox(
                    command=command,
                    cwd=cwd,
                    timeout=timeout,
                    image="python:3.11-slim"
                )
            except Exception as e:
                return {"success": False, "error": f"Sandbox execution failed: {e}"}
        else:
            from aria_code.safety import sandbox as _os_sandbox
            confined = _os_sandbox.wrap(
                argv if (argv and not use_shell) else command,
                use_shell=use_shell, mode=mode, network=network, cwd=cwd,
                setting=params.get("os_sandbox"),
                workspace=params.get("_workspace"),
                extra=params.get("_allowed_write_roots", ()),
            )
            # In a task worktree, the worktree's own source comes first on
            # PYTHONPATH, so an editable install of the user's checkout does
            # not answer imports meant for the task's edits.
            from aria_code.runtime.task_worktree import worktree_command_env
            result = subprocess.run(
                confined or (argv if (argv and not use_shell) else command),
                shell=use_shell and not confined,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=worktree_command_env(params.get("_workspace") or "", params.get("_workspace_origin") or ""),
            )
            if confined and "sandbox-exec: sandbox_apply" in (result.stderr or ""):
                # Never fall back to running it unconfined.
                return {"success": False, "error": "The command sandbox could not start: "
                        + result.stderr.strip()[:300]
                        + " — set os_sandbox=off in the config to run commands without it."}
            if confined and result.returncode != 0:
                sandbox_hint = _os_sandbox.denial_hint(result.stdout + "\n" + result.stderr,
                                                       mode=mode, network=network)
        full_stdout = result.stdout
        full_stderr = result.stderr
        output = full_stdout[-5000:] if len(full_stdout) > 5000 else full_stdout
        stderr = full_stderr[-2000:] if len(full_stderr) > 2000 else full_stderr
        output_artifact = _persist_command_output(command, full_stdout, full_stderr, result.returncode)

        # A failed command is reported, not repaired here. This used to edit
        # the script (inserting imports, rewriting DataFrame.append) and run
        # `pip3 install <the module named in the error>`, then re-run with
        # shell=True: file writes with no approval and no checkpoint, so /undo
        # could not reverse them, and a package install that ignored network
        # off and the sandbox. The model gets the error and a hint, and makes
        # the change through edit_file or an approved run_command.
        hint = sandbox_hint or (_failure_hint(output + "\n" + stderr) if result.returncode != 0 else "")


        if quiet:
            pass
        elif has_rich and console is not None:
            print_command_outcome(console, result.returncode, output, stderr,
                                  output_artifact.get("full_output_path", ""))
        else:
            print(f"  Command exit: {result.returncode}")
        data = {
            "command": command, "exit_code": result.returncode,
            "stdout": output, "stderr": stderr,
            "stdout_truncated": len(full_stdout) > 5000,
            "stderr_truncated": len(full_stderr) > 2000,
        }
        data.update(output_artifact)
        if hint:
            data["hint"] = hint
        return {"success": True, "data": data}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timed out ({timeout}s)"}
    except KeyboardInterrupt:
        _cprint("  [dim]Command interrupted[/dim]", console=console, has_rich=has_rich)
        return {"success": False, "error": "Command interrupted by user (Ctrl+C)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _start_background(params, command, argv, use_shell, cwd, mode, network, *, docker,
                      console, has_rich) -> dict:
    """Start a long-running command (server, watcher) and return its first output."""
    if docker:
        return {"success": False, "error": "background=true is not available with the Docker sandbox."}
    from aria_code.runtime import processes
    from aria_code.safety import sandbox as _os_sandbox

    target = argv if (argv and not use_shell) else command
    confined = _os_sandbox.wrap(target, use_shell=use_shell, mode=mode, network=network, cwd=cwd,
                                setting=params.get("os_sandbox"), workspace=params.get("_workspace"),
                                extra=params.get("_allowed_write_roots", ()))
    result = processes.start(confined or target, shell=use_shell and not confined, cwd=cwd,
                             label=command, wait=min(float(params.get("wait_seconds", 3) or 0), 30.0),
                             until=params.get("until"))
    if result.get("success"):
        data = result["data"]
        state = "running" if data["running"] else f"exited {data['exit_code']}"
        _cprint(f"  [dim]⎿ background {data['process_id']} · {state}[/dim]",
                console=console, has_rich=has_rich)
        if confined and not data["running"]:
            hint = _os_sandbox.denial_hint(data.get("output", ""), mode=mode, network=network)
            if hint:
                data["hint"] = hint
    return result


_PIP_NAMES = {
    "sklearn": "scikit-learn", "cv2": "opencv-python", "bs4": "beautifulsoup4",
    "PIL": "Pillow", "yaml": "PyYAML",
}


def _failure_hint(text: str) -> str:
    """What to do about a common failure, for the model to act on with approval."""
    missing = re.search(r"No module named ['\"]?([\w.]+)", text)
    if missing:
        module = missing.group(1).split(".")[0]
        package = _PIP_NAMES.get(module, module)
        return (f"Module '{module}' is not installed. If it is the right dependency, install it with "
                f"run_command (e.g. `python3 -m pip install {package}`; check the package name first).")
    name = re.search(r"NameError: name ['\"](\w+)['\"] is not defined", text)
    if name:
        return f"'{name.group(1)}' is not defined: add the missing import or definition with edit_file."
    if "DataFrame' object has no attribute 'append'" in text:
        return "DataFrame.append was removed in pandas 2: use pd.concat with edit_file."
    return ""


def tool_web_fetch(params: dict) -> dict:
    """Fetch the text content of any URL."""
    url = params.get("url", "").strip()
    if not url:
        return {"success": False, "error": "Missing 'url' parameter"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    max_chars = min(int(params.get("max_chars", 4000)), 12000)
    timeout   = min(int(params.get("timeout", 15)), 30)
    try:
        import urllib.request as _ur
        import ssl as _ssl
        _prx = _ur.getproxies()
        # SEC EDGAR rejects browser-spoofed UAs from scripts (403) and requires
        # a declarative UA with contact info — the root cause of "verify via
        # 10-Q" tasks silently degrading to aggregator sites. Honor SEC's rule
        # for sec.gov; keep a browser UA for sites that block obvious bots.
        from urllib.parse import urlparse as _urlparse
        _host = (_urlparse(url).hostname or "").lower()
        if _host == "sec.gov" or _host.endswith(".sec.gov"):
            _contact = os.environ.get("ARIA_SEC_CONTACT", "").strip() or "research@aria-code.local"
            _ua = f"aria-code research client ({_contact})"
        else:
            _ua = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        headers = {
            "User-Agent": _ua,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        }
        _gh_m = re.match(
            r"https://github\.com/([^/]+/[^/]+)/blob/([^?#]+)", url
        )
        if _gh_m:
            url = f"https://raw.githubusercontent.com/{_gh_m.group(1)}/{_gh_m.group(2)}"

        import requests as _req
        s = _req.Session()
        s.proxies = _prx
        # Certificate verification ON by default — research data fetched over
        # an unverified channel is tamperable. ARIA_WEB_FETCH_INSECURE=1 is
        # the explicit escape hatch for TLS-intercepting corporate proxies.
        s.verify = os.environ.get("ARIA_WEB_FETCH_INSECURE", "") != "1"
        r = s.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        raw = r.text

        ct = r.headers.get("content-type", "")
        if "json" in ct or raw.lstrip().startswith(("{", "[")):
            return {"success": True, "data": {
                "url": url, "content_type": ct,
                "text": raw[:max_chars], "length": len(raw),
            }}

        text = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL | re.I)
        text = re.sub(r"<style[^>]*>.*?</style>",   " ", text, flags=re.DOTALL | re.I)
        text = re.sub(r"<[^>]+>",     " ", text)
        text = re.sub(r"&nbsp;",      " ", text)
        text = re.sub(r"&amp;",       "&", text)
        text = re.sub(r"&lt;",        "<", text)
        text = re.sub(r"&gt;",        ">", text)
        text = re.sub(r"&quot;",      '"', text)
        text = re.sub(r"\s{3,}",      "\n", text)
        text = text.strip()

        return {"success": True, "data": {
            "url": url, "content_type": ct,
            "text": text[:max_chars], "length": len(text),
            "truncated": len(text) > max_chars,
        }}
    except Exception as e:
        return {"success": False, "error": f"web_fetch failed: {e}"}


def tool_github(
    params: dict,
    *,
    console=None,
    has_rich: bool = True,
) -> dict:
    """GitHub API / gh CLI integration.

    actions: list_prs, list_issues, view_pr, view_issue, create_pr,
             list_commits, read_file, search, pr_diff, pr_checks
    """
    action = params.get("action", "list_prs").lower().replace("-", "_")
    cwd    = params.get("cwd") or None
    policy = "safe"

    def _gh(cmd: str, timeout: int = 20) -> dict:
        import shutil
        if not shutil.which("gh"):
            return {"success": False,
                    "error": "gh CLI not found. Install: brew install gh && gh auth login"}
        return tool_run_command(
            {"command": cmd, "cwd": cwd, "timeout": timeout, "policy": policy},
            console=console,
            has_rich=has_rich,
        )

    if action in ("list_prs", "prs", "pull_requests"):
        state = params.get("state", "open")
        limit = int(params.get("limit", 20))
        return _gh(f"gh pr list --state {state} --limit {limit} "
                   "--json number,title,author,state,headRefName,url")

    if action in ("list_issues", "issues"):
        state = params.get("state", "open")
        limit = int(params.get("limit", 20))
        label = f' --label "{params["label"]}"' if params.get("label") else ""
        return _gh(f"gh issue list --state {state} --limit {limit}{label} "
                   "--json number,title,author,state,labels,url")

    if action in ("view_pr", "pr"):
        number = params.get("number") or params.get("pr")
        if not number:
            return {"success": False, "error": "Missing 'number' parameter"}
        return _gh(f"gh pr view {number} "
                   "--json number,title,body,state,headRefName,baseRefName,additions,deletions,files,url")

    if action in ("view_issue", "issue"):
        number = params.get("number") or params.get("issue")
        if not number:
            return {"success": False, "error": "Missing 'number' parameter"}
        return _gh(f"gh issue view {number} "
                   "--json number,title,body,state,labels,comments,url")

    if action == "create_pr":
        title  = params.get("title", "")
        body   = params.get("body", "")
        branch = params.get("branch", "")
        base   = params.get("base", "main")
        if not title:
            return {"success": False, "error": "Missing 'title' for create_pr"}
        b_flag = f"--head {shlex.quote(branch)}" if branch else ""
        cmd = (
            f"gh pr create --title {shlex.quote(title)} "
            f"--body {shlex.quote(body)} "
            f"--base {shlex.quote(base)} {b_flag}"
        )
        return _gh(cmd, timeout=30)

    if action in ("list_commits", "commits", "log"):
        limit = int(params.get("limit", 10))
        return _gh(
            f"gh api repos/{{owner}}/{{repo}}/commits?per_page={limit} "
            "--jq '[.[] | {sha: .sha[:7], "
            'message: .commit.message | split("\\n")[0], '
            "author: .commit.author.name, date: .commit.author.date}]'"
        )

    if action == "search":
        q    = params.get("q") or params.get("query", "")
        kind = params.get("kind", "code")
        if not q:
            return {"success": False, "error": "Missing 'q' parameter"}
        return _gh(f"gh search {kind} {shlex.quote(q)} --limit 10 "
                   "--json url,path,textMatches", timeout=15)

    if action in ("read_file", "file"):
        ref       = params.get("ref", "")
        file_path = params.get("path", "")
        if ref:
            m = re.match(r"([^@:]+)@([^:]+):(.+)", ref)
            if m:
                repo, branch, fp = m.groups()
                url = f"https://raw.githubusercontent.com/{repo}/{branch}/{fp}"
                return tool_web_fetch({"url": url, "max_chars": 20000})
        if file_path:
            return _gh(f"gh api repos/{{owner}}/{{repo}}/contents/{file_path} "
                       "--jq '.content' | base64 -d")
        return {"success": False, "error": "Provide 'ref' (owner/repo@branch:path) or 'path'"}

    if action in ("pr_diff", "diff"):
        number = params.get("number") or params.get("pr")
        if not number:
            return {"success": False, "error": "Missing 'number' parameter"}
        return _gh(f"gh pr diff {number}", timeout=30)

    if action in ("pr_checks", "checks", "ci"):
        number = params.get("number") or params.get("pr")
        return _gh(f"gh pr checks {number or ''}")

    if action in ("git_status", "status"):
        return tool_run_command(
            {"command": "git status --short && echo '---' && git log --oneline -5",
             "cwd": cwd, "policy": policy},
            console=console, has_rich=has_rich,
        )

    if action in ("commit_and_push", "commit", "push"):
        message   = params.get("message", "")
        branch    = params.get("branch", "")
        add_files = params.get("files", [])
        repo      = params.get("repo", "")    # e.g. "artheras/aria-code"
        coauthor  = params.get("coauthor", "")

        if not message:
            return {"success": False, "error": "Missing 'message' for commit_and_push"}

        # ── Aria Code[bot] identity via GitHub App ─────────────────────────────
        try:
            from aria_code.apps.cli.github_app_auth import (
                get_installation_token, get_aria_git_url,
                aria_bot_env, ARIA_BOT_NAME, ARIA_BOT_EMAIL,
            )
            owner = (repo.split("/")[0] if repo else None) or "artheras"
            token = get_installation_token(owner)
            bot_env = aria_bot_env()
            auth_available = True
        except Exception as _auth_err:
            # Fall back to local git credentials if App not configured
            token = None
            bot_env = {
                "GIT_AUTHOR_NAME":     "Aria Code",
                "GIT_AUTHOR_EMAIL":    "aria-code[bot]@artherahq.com",
                "GIT_COMMITTER_NAME":  "Aria Code",
                "GIT_COMMITTER_EMAIL": "aria-code[bot]@artherahq.com",
            }
            auth_available = False
            ARIA_BOT_NAME  = "Aria Code"
            ARIA_BOT_EMAIL = "aria-code[bot]@artherahq.com"

        env_prefix = " ".join(f'{k}="{v}"' for k, v in bot_env.items()) + " "

        # ── Build commit message with co-author attribution ────────────────────
        body = message
        if coauthor:
            body += f"\n\nCo-Authored-By: {coauthor}"
        # Always credit the Aria GitHub account so it appears in Contributors
        if auth_available:
            try:
                from aria_code.apps.cli.github_app_auth import ARIA_GITHUB_LOGIN, ARIA_GITHUB_EMAIL
                body += f"\n\nCo-Authored-By: {ARIA_GITHUB_LOGIN} <{ARIA_GITHUB_EMAIL}>"
            except ImportError:
                pass
        user_name  = tool_run_command({"command": "git config user.name",  "policy": "safe", "cwd": cwd}).get("output", "").strip()
        user_email = tool_run_command({"command": "git config user.email", "policy": "safe", "cwd": cwd}).get("output", "").strip()
        if user_name and user_email and user_email != ARIA_BOT_EMAIL:
            body += f"\n\nCo-Authored-By: {user_name} <{user_email}>"

        # ── Stage ──────────────────────────────────────────────────────────────
        stage_cmd = ("git add " + " ".join(shlex.quote(f) for f in add_files)) if add_files else "git add -A"
        stage_result = tool_run_command(
            {"command": stage_cmd, "cwd": cwd, "policy": policy},
            console=console, has_rich=has_rich,
        )
        if not stage_result.get("success"):
            return stage_result

        # ── Commit ─────────────────────────────────────────────────────────────
        safe_body = body.replace('"', '\\"').replace('$', '\\$')
        commit_result = tool_run_command(
            {"command": f'{env_prefix}git commit -m "{safe_body}"',
             "cwd": cwd, "policy": policy},
            console=console, has_rich=has_rich,
        )
        if not commit_result.get("success"):
            return commit_result

        # ── Push (use App token remote if available) ───────────────────────────
        push_branch = branch or tool_run_command(
            {"command": "git rev-parse --abbrev-ref HEAD", "policy": "safe", "cwd": cwd}
        ).get("output", "").strip() or "main"

        if auth_available and token and repo:
            auth_url   = get_aria_git_url(repo, token)
            push_cmd   = f"git push {shlex.quote(auth_url)} {shlex.quote(push_branch)}"
        else:
            push_cmd   = f"git push origin {shlex.quote(push_branch)}"

        push_result = tool_run_command(
            {"command": push_cmd, "cwd": cwd, "policy": policy, "timeout": 60},
            console=console, has_rich=has_rich,
        )
        return {
            **push_result,
            "committed_as": f"{ARIA_BOT_NAME} <{ARIA_BOT_EMAIL}>",
            "branch": push_branch,
            "app_auth": auth_available,
        }

    return {
        "success": False,
        "error": (
            f"Unknown GitHub action: '{action}'. "
            "Use: list_prs, list_issues, view_pr, view_issue, create_pr, "
            "list_commits, search, read_file, pr_diff, pr_checks, "
            "git_status, commit_and_push"
        ),
    }


def tool_ask_user(params: dict, *, console=None, has_rich: bool = True) -> dict:
    """Pause execution and ask the user for clarification or a decision."""
    question = params.get("question", "")
    if not question:
        return {"success": False, "error": "Missing 'question' parameter"}
    
    if has_rich and console is not None:
        from rich.prompt import Prompt
        console.print(f"\n[bold magenta]🤔 Agent needs clarification:[/bold magenta] {question}")
        try:
            answer = Prompt.ask("[cyan]Your answer[/cyan]")
        except (KeyboardInterrupt, EOFError):
            return {"success": False, "error": "User cancelled the input."}
    else:
        print(f"\nAgent needs clarification: {question}")
        try:
            answer = input("Your answer: ")
        except (KeyboardInterrupt, EOFError):
            return {"success": False, "error": "User cancelled the input."}
            
    return {"success": True, "data": {"user_answer": answer}}
