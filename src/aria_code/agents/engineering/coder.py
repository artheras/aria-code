"""
agents/engineering/coder.py — Coder Agent
=========================================
Engineering agent responsible for translating Quant Strategist Rule Trees
into self-contained, robust, executable Python / TypeScript backtest scripts.
Never dumps raw unexecuted code into user dialogue; saves directly to disk.
"""

from __future__ import annotations

import inspect
import logging
import os
import pathlib
import re
import shlex
import subprocess
from typing import Any, Awaitable, Callable, Dict, Optional

from ..base import BaseAgent, AgentResult

logger = logging.getLogger(__name__)


class CoderAgent(BaseAgent):
    name = "coder"
    description = "编码智能体 — 在指定工作区读写文件；执行命令需宿主明确授权"

    def __init__(
        self,
        output_dir: Optional[pathlib.Path] = None,
        llm_provider=None,
        data_router=None,
        on_token: Optional[Callable[[str], None]] = None,
        on_thought: Optional[Callable[[str], None]] = None,
        on_tool_start: Optional[Callable[[str, Dict], None]] = None,
        on_tool_end: Optional[Callable[[str, Any], None]] = None,
        lang: str = "zh",
        command_approval: Optional[Callable[[list[str], pathlib.Path], bool | Awaitable[bool]]] = None,
        on_user_question: Optional[Callable[[str], str | Awaitable[str]]] = None,
    ) -> None:
        super().__init__(
            llm_provider=llm_provider,
            data_router=data_router,
            on_token=on_token,
            on_thought=on_thought,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
            lang=lang,
        )
        self.output_dir = (output_dir or pathlib.Path.cwd() / "generated").resolve()
        self.command_approval = command_approval
        self.on_user_question = on_user_question

    def _workspace_path(self, raw_path: str, *, required: bool = True) -> pathlib.Path:
        """Resolve a model-supplied path without allowing access outside the workspace."""
        if required and not raw_path:
            raise ValueError("A workspace-relative path is required")
        path = pathlib.Path(raw_path)
        if path.is_absolute():
            raise ValueError("Absolute paths are not allowed")
        resolved = (self.output_dir / path).resolve()
        if not resolved.is_relative_to(self.output_dir):
            raise ValueError("Path escapes the workspace")
        return resolved

    async def _execute_tool(self, tool_name: str, tool_args: Dict[str, Any]) -> str:
        if self.on_tool_start:
            self.on_tool_start(tool_name, tool_args)

        try:
            if tool_name == "run_command":
                argv = shlex.split(str(tool_args.get("command", "")))
                if not argv:
                    raise ValueError("Command is required")
                cwd = self._workspace_path(str(tool_args.get("cwd", "")), required=False)
                if not cwd.is_dir():
                    raise ValueError("Command directory does not exist")
                if self.command_approval is None:
                    result = "Error: command approval is unavailable; command was not run"
                else:
                    approved = self.command_approval(argv, cwd)
                    if inspect.isawaitable(approved):
                        approved = await approved
                    if not approved:
                        result = "Error: command was denied; command was not run"
                    else:
                        proc = subprocess.run(argv, shell=False, cwd=cwd, capture_output=True, text=True, timeout=120)
                        result = f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}\nReturnCode: {proc.returncode}"

            elif tool_name in {"write_file", "read_file", "edit_file_chunk"}:
                path = self._workspace_path(str(tool_args.get("filename", "")))
                if tool_name == "write_file":
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(str(tool_args.get("content", "")), encoding="utf-8")
                    result = f"Successfully wrote full file to {path}"
                elif tool_name == "read_file":
                    lines = path.read_text(encoding="utf-8").splitlines()
                    result = f"File: {path}\n" + "\n".join(f"{i:04d} | {line}" for i, line in enumerate(lines, 1))
                else:
                    target = str(tool_args.get("target_content", ""))
                    if not target:
                        raise ValueError("target_content is required")
                    original = path.read_text(encoding="utf-8")
                    if target not in original:
                        result = f"Error: target_content not found in {path}"
                    else:
                        path.write_text(original.replace(target, str(tool_args.get("replacement_content", ""))), encoding="utf-8")
                        result = f"Successfully replaced chunk in {path}"

            elif tool_name == "search_code":
                query = str(tool_args.get("query", ""))
                if not query:
                    raise ValueError("Search query is required")
                matches: list[str] = []
                for root, dirs, files in os.walk(self.output_dir, followlinks=False):
                    dirs[:] = [name for name in dirs if not name.startswith(".")]
                    for filename in files:
                        if len(matches) >= 50:
                            break
                        path = pathlib.Path(root) / filename
                        try:
                            if filename.startswith(".") or not path.resolve().is_relative_to(self.output_dir) or path.stat().st_size > 1_000_000:
                                continue
                            lines = path.read_text(encoding="utf-8").splitlines()
                        except (OSError, UnicodeError):
                            continue
                        for line_no, line in enumerate(lines, 1):
                            if query in line:
                                matches.append(f"{path.relative_to(self.output_dir)}:{line_no}:{line}")
                                if len(matches) >= 50:
                                    break
                    if len(matches) >= 50:
                        break
                result = "\n".join(matches) if matches else "No matches found."

            elif tool_name == "list_dir":
                path = self._workspace_path(str(tool_args.get("path", "")), required=False)
                if not path.is_dir():
                    raise ValueError("Directory does not exist")
                items: list[str] = []
                for root, dirs, files in os.walk(path, followlinks=False):
                    depth = len(pathlib.Path(root).relative_to(path).parts)
                    if depth >= 3:
                        dirs[:] = []
                    dirs[:] = [name for name in dirs if not name.startswith(".")]
                    items.extend(str((pathlib.Path(root) / name).relative_to(path)) for name in dirs + files if not name.startswith("."))
                    if len(items) >= 200:
                        break
                result = "\n".join(items[:200])

            elif tool_name == "take_screenshot":
                result = "Error: screenshot capability is not configured; no image was captured"

            elif tool_name == "github_api":
                repo = str(tool_args.get("repo", ""))
                issue = str(tool_args.get("issue_number", ""))
                if tool_args.get("action") != "read_issue" or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not issue.isdecimal():
                    raise ValueError("Only read_issue with a valid repository and issue number is supported")
                proc = subprocess.run(["gh", "issue", "view", issue, "--repo", repo], shell=False, capture_output=True, text=True, timeout=30)
                result = proc.stdout if proc.returncode == 0 else proc.stderr

            elif tool_name == "ask_user":
                question = str(tool_args.get("question", ""))
                if self.on_user_question is None:
                    result = "Error: user interaction is unavailable; no user response was received"
                else:
                    answer = self.on_user_question(question)
                    if inspect.isawaitable(answer):
                        answer = await answer
                    result = f"USER REPLIED: {answer}"
            else:
                result = await super()._execute_tool(tool_name, tool_args)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            result = f"Tool Error: {exc}"

        if self.on_tool_end:
            self.on_tool_end(tool_name, result)
        return result

    async def analyze(self, symbol: str, data: Dict[str, Any]) -> AgentResult:
        """
        Advanced Autonomous Software Engineer Loop.
        """
        rule_tree = data.get("strategy_rules") or data.get("rule_tree")
        request_text = data.get("request", "Write a Python script.")
        
        system_prompt = (
            "You are a software engineer working within a bounded local workspace. "
            "Use workspace-relative paths only. Never claim an action succeeded without a tool result.\n"
            "Tools: list_dir(path), search_code(query), read_file(filename), "
            "write_file(filename, content), edit_file_chunk(filename, target_content, replacement_content), "
            "github_api(action='read_issue', repo='owner/repo', issue_number='123').\n"
            "run_command(command, cwd) executes an argv command only after host approval. "
            "If approval is unavailable or denied, do not retry to bypass it; report that verification was not run. "
            "Shell pipelines, redirects and command substitution are unavailable.\n"
            "Screenshot capture is unavailable; do not claim visual verification.\n"
            + ("ask_user(question) forwards a real question to the host.\n" if self.on_user_question else "User interaction is unavailable; do not invent a reply.\n")
            + "Call a tool with one JSON block per turn: "
            + '{"type":"tool_call","name":"<tool_name>","args":{}}\n'
            + "Inspect context, make focused edits, request approved verification, and report observed results."
        )

        user_prompt = f"Workspace: {self.output_dir}\nTarget Symbol: {symbol}\nRules: {rule_tree}\nUser Request: {request_text}\n\nPlease begin autonomous execution."
        
        analysis = await self._call_llm(system_prompt, user_prompt, max_tokens=2500, max_tool_loops=15)
        
        report = (
            f"### Aria Coder - Engineering Report\n\n"
            f"**项目根目录**：`{self.output_dir.absolute()}`\n\n"
            f"**执行记录与结果**：\n{analysis}\n"
        )
        
        return AgentResult(
            agent=self.name,
            symbol=symbol,
            analysis=report,
            confidence=0.5,
            signal="HOLD",
            key_points=[],
            data_used={"workspace": str(self.output_dir)},
        )
