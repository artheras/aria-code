"""Code review as a service: one target, one isolated reviewer, structured findings.

Shared by the interactive ``/review`` command and the headless
``aria-code review`` used in CI. The design follows the review flow of
OpenAI's Codex CLI (Apache-2.0):

- **A named target.** Uncommitted changes (tracked *and* untracked), staged
  changes, everything since the merge base with a branch, one commit, or one
  file. The old ``/review`` saw ``git diff HEAD`` only, so new files were never
  reviewed, and a branch could not be reviewed against ``main`` at all.
- **An isolated reviewer.** The model gets the rubric, the project's own rules
  and the change — no conversation history and no tools — so a review cannot
  run commands, edit files, or lean on earlier chat.
- **Structured output.** Every finding has a priority (P0–P3), a confidence and
  a file + line range; the review ends with an overall verdict. Parsing
  degrades in steps (JSON → the first ``{…}`` in the text → the text as the
  explanation), so a model that drifts from the format still yields a review.
- **Nothing hidden.** A change too large for one review is cut at a file
  boundary and the reviewer is told which files it did not see; the old
  command truncated silently at 12,000 characters.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)

PRIORITIES = ("P0", "P1", "P2", "P3")
DEFAULT_MAX_CHARS = 60_000
MAX_UNTRACKED_FILES = 40
MAX_UNTRACKED_BYTES = 200_000
PROJECT_RULE_FILES = ("AGENTS.override.md", "AGENTS.md", "ARIA.md")
MAX_RULE_CHARS = 8_000


class ReviewError(RuntimeError):
    """The review could not be prepared or run (not a git repo, unknown branch, model failure)."""


# ── Target and input ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReviewTarget:
    kind: str                 # uncommitted | staged | base | commit | file
    value: str = ""           # branch, commit or path

    def describe(self) -> str:
        return {
            "uncommitted": "uncommitted changes (staged, unstaged and untracked)",
            "staged": "staged changes",
            "base": f"changes against {self.value}",
            "commit": f"commit {self.value}",
            "file": f"file {self.value}",
        }[self.kind]


@dataclass
class ReviewInput:
    target: ReviewTarget
    root: Path
    diff: str
    files: list[str]
    omitted_files: list[str] = field(default_factory=list)
    total_chars: int = 0
    added: int = 0
    removed: int = 0
    base_sha: str = ""
    commit_title: str = ""
    # New-side line ranges each file's hunks cover, to tell a finding on the
    # change from one elsewhere in the file.
    hunks: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        return bool(self.omitted_files)


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                            errors="replace", timeout=60)
    if check and result.returncode != 0:
        raise ReviewError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def repo_root(cwd: Path) -> Path:
    try:
        return Path(_git(cwd, "rev-parse", "--show-toplevel").strip())
    except (ReviewError, FileNotFoundError) as exc:
        raise ReviewError(f"{cwd} is not inside a git repository") from exc


def _has_head(root: Path) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=root,
                          capture_output=True).returncode == 0


def _untracked_as_diff(root: Path) -> str:
    names = [n for n in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if n]
    parts = []
    for name in names[:MAX_UNTRACKED_FILES]:
        path = root / name
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8000] or len(data) > MAX_UNTRACKED_BYTES:
            parts.append(f"diff --git a/{name} b/{name}\nnew file (binary or larger than "
                         f"{MAX_UNTRACKED_BYTES} bytes; not shown)\n")
            continue
        lines = data.decode("utf-8", "replace").splitlines()
        body = "\n".join("+" + line for line in lines)
        parts.append(f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n+++ b/{name}\n"
                     f"@@ -0,0 +1,{len(lines)} @@\n{body}\n")
    if len(names) > MAX_UNTRACKED_FILES:
        parts.append(f"# {len(names) - MAX_UNTRACKED_FILES} more untracked files not shown\n")
    return "".join(parts)


_FILE_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.M)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def _split_files(diff: str) -> list[tuple[str, str]]:
    starts = [(m.start(), m.group(2)) for m in _FILE_HEADER.finditer(diff)]
    return [(name, diff[start:(starts[i + 1][0] if i + 1 < len(starts) else len(diff))])
            for i, (start, name) in enumerate(starts)]


def _hunks(section: str) -> list[tuple[int, int]]:
    ranges = []
    for match in _HUNK.finditer(section):
        start, count = int(match.group(1)), int(match.group(2) or 1)
        if count:
            ranges.append((start, start + count - 1))
    return ranges


def _fit(target: ReviewTarget, root: Path, diff: str, max_chars: int, **extra) -> ReviewInput:
    sections = _split_files(diff)
    kept, omitted, size = [], [], 0
    for name, section in sections:
        if size + len(section) > max_chars and kept:
            omitted.append(name)
            continue
        kept.append((name, section))
        size += len(section)
    text = "".join(section for _, section in kept)
    added = sum(1 for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in text.splitlines() if line.startswith("-") and not line.startswith("---"))
    return ReviewInput(target=target, root=root, diff=text, files=[n for n, _ in kept], omitted_files=omitted,
                       total_chars=len(diff), added=added, removed=removed,
                       hunks={n: _hunks(s) for n, s in kept}, **extra)


def collect(target: ReviewTarget, cwd: Path | str = ".", *, max_chars: int = DEFAULT_MAX_CHARS) -> ReviewInput:
    """Gather exactly what the target names, as a unified diff."""
    cwd = Path(cwd)
    if target.kind == "file":
        path = (cwd / target.value).resolve()
        if not path.is_file():
            raise ReviewError(f"file not found: {target.value}")
        try:
            root = repo_root(path.parent)
            name = str(path.relative_to(root))
        except ReviewError:
            root, name = path.parent, path.name
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        diff = (f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1,{len(lines)} +1,{len(lines)} @@\n"
                + "".join(f" {line}\n" for line in lines))
        return _fit(target, root, diff, max_chars)

    root = repo_root(cwd)
    if target.kind == "uncommitted":
        tracked = _git(root, "diff", "HEAD") if _has_head(root) else _git(root, "diff", "--cached")
        return _fit(target, root, tracked + _untracked_as_diff(root), max_chars)
    if target.kind == "staged":
        return _fit(target, root, _git(root, "diff", "--cached"), max_chars)
    if target.kind == "base":
        branch = target.value
        if not branch:
            raise ReviewError("--base needs a branch name")
        upstream = _git(root, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}", check=False).strip()
        base_sha = ""
        for candidate in (upstream, branch):
            if candidate:
                base_sha = _git(root, "merge-base", "HEAD", candidate, check=False).strip()
                if base_sha:
                    break
        if not base_sha:
            raise ReviewError(f"no merge base between HEAD and {branch}")
        return _fit(target, root, _git(root, "diff", base_sha), max_chars, base_sha=base_sha)
    if target.kind == "commit":
        sha = _git(root, "rev-parse", "--verify", f"{target.value}^{{commit}}").strip()
        title = _git(root, "log", "-1", "--format=%s", sha).strip()
        patch = _git(root, "show", "--format=", "--patch", sha)
        return _fit(target, root, patch, max_chars, base_sha=sha, commit_title=title)
    raise ReviewError(f"unknown review target {target.kind!r}")


def project_rules(root: Path) -> str:
    """The project's own review guidance, most specific file first."""
    for name in PROJECT_RULE_FILES:
        path = root / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return f"{name}:\n{text[:MAX_RULE_CHARS]}"
    return ""


# ── The reviewer ─────────────────────────────────────────────────────────────

RUBRIC = """You review one code change for the engineer who wrote it. Report only problems they would want to fix before merging.

What counts as a finding:
- It changes behaviour for the worse: a wrong result, a crash, lost or leaked data, a security hole, a serious slowdown, or code that will clearly be hard to maintain.
- It was introduced by this change. Problems that were already there are out of scope.
- It is specific: one problem, one place, one fix. Say which inputs, settings or environments trigger it; if it depends on them, say so in the first sentence.
- It is shown, not guessed. "This might affect X" is not a finding unless you can point to the code in X that breaks.
- It is not an obvious, deliberate choice by the author.

What does not: style, naming, formatting, typos and missing comments, unless a documented project rule requires them.

Priorities, at the start of each title:
[P0] blocks a release or breaks the main path for everyone, whatever the input.
[P1] urgent: fix in the next cycle.
[P2] normal: fix when convenient.
[P3] minor.

Each finding: a title of at most 80 characters that names the problem; a body of one short paragraph saying why it is wrong and when it happens; at most three lines of code, in backticks; the narrowest line range (new-side line numbers of the change, no more than about ten lines); and a confidence from 0 to 1. List every qualifying problem — and none when there are none. A short empty review is better than padding.

Then give an overall verdict: "patch is correct" when existing code and tests will keep working and nothing above P3 is wrong, otherwise "patch is incorrect", with a one-to-three sentence explanation and a confidence.

Respond with JSON only, no prose and no code fences, in exactly this shape:
{"findings": [{"title": "[P1] ...", "body": "...", "confidence_score": 0.8, "priority": 1,
  "code_location": {"file_path": "path/relative/to/repo", "line_range": {"start": 10, "end": 12}}}],
 "overall_correctness": "patch is correct" | "patch is incorrect",
 "overall_explanation": "...",
 "overall_confidence_score": 0.8}"""

LANGUAGE_NOTE = {
    "en": "Write titles, bodies and the explanation in English.",
    "zh": "标题、正文和总体说明用简体中文书写；JSON 字段名和优先级标签保持英文。",
}


def build_messages(inp: ReviewInput, *, lang: str = "en", automated_hints: str = "",
                   rules: str | None = None) -> list[dict]:
    """The reviewer's whole world: rubric, project rules, the change. Nothing else."""
    rules = project_rules(inp.root) if rules is None else rules
    system = RUBRIC + "\n\n" + LANGUAGE_NOTE["zh" if lang.startswith("zh") else "en"]
    if rules:
        system += ("\n\nThis project's own guidance follows. It overrides the defaults above where they "
                   "conflict; when a finding rests on it, cite the file.\n\n" + rules)
    parts = [f"Review target: {inp.target.describe()}."]
    if inp.commit_title:
        parts.append(f'Commit title: "{inp.commit_title}".')
    if inp.base_sha and inp.target.kind == "base":
        parts.append(f"Merge base: {inp.base_sha}.")
    parts.append(f"Files: {', '.join(inp.files) or '(none)'} (+{inp.added} -{inp.removed}).")
    if inp.truncated:
        parts.append(f"The change was too large to show whole. These files were NOT shown and must not be "
                     f"reviewed or guessed about: {', '.join(inp.omitted_files)}.")
    if automated_hints:
        parts.append("Automated pattern checks flagged the lines below. Keep a hint only if the change really "
                     "has the problem; they are leads, not findings.\n" + automated_hints)
    parts.append("The change:\n" + inp.diff)
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]


@dataclass
class ReviewFinding:
    title: str
    body: str
    priority: int | None
    confidence: float
    file_path: str
    start: int
    end: int
    in_change: bool = True

    @property
    def label(self) -> str:
        return PRIORITIES[self.priority] if self.priority is not None else "P?"

    @property
    def location(self) -> str:
        return f"{self.file_path}:{self.start}" + (f"-{self.end}" if self.end != self.start else "")


@dataclass
class ReviewResult:
    findings: list[ReviewFinding]
    correct: bool | None
    explanation: str
    confidence: float
    structured: bool = True              # False when the model's reply was not the JSON asked for
    target: str = ""
    files: list[str] = field(default_factory=list)
    omitted_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "files": self.files,
            "omitted_files": self.omitted_files,
            "overall_correctness": None if self.correct is None else
            ("patch is correct" if self.correct else "patch is incorrect"),
            "overall_explanation": self.explanation,
            "overall_confidence_score": self.confidence,
            "structured": self.structured,
            "findings": [{**asdict(f), "priority_label": f.label} for f in self.findings],
        }

    def worst_priority(self) -> int | None:
        levels = [f.priority for f in self.findings if f.priority is not None and f.in_change]
        return min(levels) if levels else None


def _clamp(value, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _priority(raw: dict) -> int | None:
    value = raw.get("priority")
    if isinstance(value, (int, float)) and 0 <= int(value) <= 3:
        return int(value)
    match = re.match(r"\s*\[P([0-3])\]", str(raw.get("title", "")))
    return int(match.group(1)) if match else None


def _load_json(text: str):
    text = text.strip()
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        candidates.append(fenced.group(1))
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{"):text.rfind("}") + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def parse_review(text: str, inp: ReviewInput | None = None) -> ReviewResult:
    data = _load_json(text or "")
    if data is None:
        return ReviewResult([], None, (text or "").strip() or "The reviewer returned nothing.", 0.0,
                            structured=False)
    findings = []
    for raw in data.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        location = raw.get("code_location") or {}
        lines = location.get("line_range") or {}
        path = str(location.get("file_path") or location.get("absolute_file_path") or "").strip()
        if inp is not None and path:
            try:
                path = str(Path(path).resolve().relative_to(inp.root.resolve())) if Path(path).is_absolute() else path
            except ValueError:
                pass
        try:
            start = int(lines.get("start") or 0)
            end = int(lines.get("end") or start)
        except (TypeError, ValueError):
            start = end = 0
        start, end = min(start, end), max(start, end)
        title = re.sub(r"^\s*\[P[0-3?]\]\s*", "", str(raw.get("title", "")).strip())
        finding = ReviewFinding(title=title[:120], body=str(raw.get("body", "")).strip(), priority=_priority(raw),
                                confidence=_clamp(raw.get("confidence_score")), file_path=path, start=start, end=end)
        if inp is not None:
            ranges = inp.hunks.get(path)
            finding.in_change = bool(ranges) and any(start <= b and end >= a for a, b in ranges)
        findings.append(finding)
    findings.sort(key=lambda f: (not f.in_change, 9 if f.priority is None else f.priority, -f.confidence))
    verdict = str(data.get("overall_correctness", "")).strip().lower()
    correct = True if verdict == "patch is correct" else False if verdict == "patch is incorrect" else None
    result = ReviewResult(findings, correct, str(data.get("overall_explanation", "")).strip(),
                          _clamp(data.get("overall_confidence_score"), 0.0))
    if inp is not None:
        result.target, result.files, result.omitted_files = inp.target.describe(), inp.files, inp.omitted_files
    return result


ModelCall = Callable[[list], Awaitable[str]]


def automated_hints(inp: ReviewInput) -> str:
    """Aria's deterministic pattern checks over the added lines, as leads for the reviewer."""
    try:
        from aria_code.agents.code_review import CodeReviewAgent
    except Exception:
        logger.debug("CodeReviewAgent unavailable for automated_hints", exc_info=True)
        return ""
    rows = []
    for name, section in _split_files(inp.diff):
        for item in CodeReviewAgent.review_source(section, filename=name, is_diff=True):
            if item.rule in ("empty-input", "input-truncated"):
                continue
            rows.append(f"- {name}: [{item.severity}] {item.rule}: {item.message} `{item.evidence[:80]}`")
    return "\n".join(rows[:30])


async def run_review(inp: ReviewInput, call_model: ModelCall, *, lang: str = "en") -> ReviewResult:
    if not inp.diff.strip():
        result = ReviewResult([], True, "Nothing to review: the target has no changes.", 1.0)
        result.target = inp.target.describe()
        return result
    reply = await call_model(build_messages(inp, lang=lang, automated_hints=automated_hints(inp)))
    return parse_review(reply, inp)


# ── Presentation ─────────────────────────────────────────────────────────────

_TEXT = {
    "en": {"title": "Review", "correct": "Looks correct", "incorrect": "Needs changes",
           "unknown": "No verdict", "none": "No findings.", "outside": "Outside this change",
           "omitted": "Not reviewed (too large to include)", "confidence": "confidence",
           "unstructured": "The reviewer did not answer in the expected format; its reply:"},
    "zh": {"title": "审查", "correct": "可以合并", "incorrect": "需要修改",
           "unknown": "无结论", "none": "没有发现问题。", "outside": "不在本次改动范围内",
           "omitted": "未审查（改动过大未纳入）", "confidence": "置信度",
           "unstructured": "审查模型未按约定格式回答，原文如下："},
}


def render_text(result: ReviewResult, *, lang: str = "en") -> str:
    t = _TEXT["zh" if lang.startswith("zh") else "en"]
    lines = [f"{t['title']} · {result.target}" if result.target else t["title"]]
    if not result.structured:
        return "\n".join(lines + [t["unstructured"], result.explanation])
    verdict = t["unknown"] if result.correct is None else t["correct"] if result.correct else t["incorrect"]
    lines.append(f"{verdict} · {t['confidence']} {result.confidence:.2f}")
    if result.explanation:
        lines.append(result.explanation)
    if not result.findings:
        lines.append(t["none"])
    shown_outside = False
    for number, f in enumerate(result.findings, 1):
        if not f.in_change and not shown_outside:
            lines.append(f"— {t['outside']} —")
            shown_outside = True
        lines.append("")
        lines.append(f"{number}. [{f.label}] {f.title}  {f.location}  ({f.confidence:.2f})")
        lines.extend("   " + line for line in f.body.splitlines() if line.strip())
    if result.omitted_files:
        lines.append("")
        lines.append(f"{t['omitted']}: {', '.join(result.omitted_files)}")
    return "\n".join(lines)


def history_note(result: ReviewResult) -> str:
    """What the main conversation remembers, so "fix finding 2" means something."""
    rows = [f"Code review of {result.target}: "
            + ("no verdict" if result.correct is None else "patch is correct" if result.correct else "patch is incorrect")
            + (f". {result.explanation}" if result.explanation else "")]
    for number, f in enumerate(result.findings, 1):
        rows.append(f"{number}. [{f.label}] {f.title} — {f.location}: {f.body}")
    return "\n".join(rows)


def parse_priority(value: str) -> int:
    value = value.strip().upper()
    if value not in PRIORITIES:
        raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
    return PRIORITIES.index(value)


def target_from_args(args: Iterable[str]) -> ReviewTarget:
    """`--staged` | `--base BRANCH` | `--commit SHA` | `PATH` | nothing (uncommitted)."""
    args = list(args)
    if not args:
        return ReviewTarget("uncommitted")
    head = args[0]
    if head in ("--staged", "--cached"):
        return ReviewTarget("staged")
    if head in ("--uncommitted",):
        return ReviewTarget("uncommitted")
    if head in ("--base", "--commit"):
        if len(args) < 2:
            raise ReviewError(f"{head} needs a value")
        return ReviewTarget(head[2:], args[1])
    if head.startswith("--"):
        raise ReviewError(f"unknown option {head}")
    return ReviewTarget("file", head)
