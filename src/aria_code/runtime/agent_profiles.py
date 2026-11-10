"""Domain-neutral task roles for the existing Aria runtime.

Profiles narrow a task's instructions and available tools. They do not provide
credentials, install integrations, choose another model, or grant permissions.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentProfile:
    name: str
    description: str
    instructions: str
    tools: frozenset[str] | None = None
    writable: bool = False
    max_rounds: int = 20
    timeout_seconds: int = 300


READ = frozenset({"read_file", "list_files", "search_code", "glob", "analyze_file", "notebook_read", "lsp_diagnostics"})
RESEARCH = READ | {"web_search", "web_fetch"}
WRITE = READ | {"write_file", "edit_file", "multi_edit", "apply_patch", "notebook_edit", "run_command"}

PROFILES = {
    p.name: p for p in (
        AgentProfile("general", "General-purpose fallback for tasks that do not fit a narrower role.",
                     "Complete the delegated goal using available tools. State missing capabilities and evidence explicitly.",
                     writable=True, max_rounds=30),
        AgentProfile("explorer", "Find and explain files, architecture and execution paths without edits.",
                     "Read targeted sources, trace the actual execution path, and cite files and symbols. Do not implement changes.", READ),
        AgentProfile("planner", "Turn a goal into an evidence-based plan, dependencies and acceptance criteria.",
                     "Inspect the relevant context. Return a concrete plan, dependencies, assumptions and observable acceptance criteria. Do not implement it.", RESEARCH),
        AgentProfile("researcher", "Research technical, product or business questions with source evidence.",
                     "Search and read primary sources when available. Separate sourced facts, inferences and unknowns; include source locations and dates for time-sensitive claims. If search access is absent, say so.", RESEARCH),
        AgentProfile("implementer", "Implement and test a bounded software change in an isolated workspace.",
                     "Read relevant code before editing. Implement the requested scope and run appropriate checks. Report changed files, actual commands and failures. Never push, publish or deploy.", WRITE, writable=True, max_rounds=30),
        AgentProfile("reviewer", "Independently inspect correctness, security and missing validation without edits.",
                     "Review the actual sources and available evidence. Report actionable findings with file locations, severity and reasoning. Distinguish static review from executed tests; no findings does not prove correctness.", READ),
        AgentProfile("data_analyst", "Analyze datasets, validate quality and produce reproducible calculations across domains.",
                     "Inspect supplied data and its grain, schema and quality. Use verified tool outputs for calculations; never invent computed totals. Use command execution only under inherited host policy. Write outputs only when workspace-write was requested. If no computation tool is available, return a reproducible method and the limitation instead of pretending to execute it.", WRITE, writable=True),
        AgentProfile("writer", "Draft and revise documents, specifications and content grounded in supplied sources.",
                     "Use authorized context and source files. Preserve audience, voice and factual provenance. Write requested files only in the permitted workspace and read them back. Return drafts for review; never send messages or publish content.", WRITE, writable=True),
    )
}


def get_profile(name: str = "general") -> AgentProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"Unknown agent profile: {name}. Use list_agents to discover supported roles.") from None


def tool_list_agents(params: dict) -> dict:
    return {"success": True, "agents": [
        {"name": p.name, "description": p.description,
         "modes": ["read-only", "workspace-write"] if p.writable else ["read-only"],
         "tool_policy": "inherit parent minus delegation" if p.tools is None else sorted(p.tools),
         "max_rounds": p.max_rounds, "timeout_seconds": p.timeout_seconds}
        for p in PROFILES.values()
    ], "note": "Profiles use the existing Aria model/runtime. Tools are intersected with the host's actual tool registry; listed roles do not imply connected services. Command execution remains subject to the host permission policy."}


DELEGATION_GUIDANCE = """General task delegation:
- Aria handles software, research, data inspection, documents and other authorized tasks across domains.
- Built-in roles: explorer (source exploration), planner (plans), researcher (sourced research), implementer (software changes), reviewer (independent review), data_analyst (datasets), writer (documents); general is the fallback.
- Use list_agents to discover reusable roles; select one by its description and the task context, not a keyword router. Use spawn_task(agent=..., prompt=..., success_criteria=...) for a bounded independent task when delegation is useful. Keep simple/dependent work in the main conversation unless the user explicitly requests a delegate. Honor explicit delegation requests: do not replace a requested independent writer/researcher task with a draft produced in the parent conversation.
- Discover relevant project skill/memory descriptions with list_task_context(agent=target_role), then pass only needed IDs in spawn_task.context_refs. Only explicit project auto entries load by default; no global memory scan occurs. Context provenance records source hashes, not proof of current facts. Never copy unrelated personal context into a delegate.
- Give the delegate its goal, relevant source context and observable acceptance criteria. Inherit the current model and permissions; never delegate to bypass a denial or gain access.
- Declare observable checks in the spawn_task checks argument when the goal permits artifact, tool or command predicates. Describing these checks in prompt/context does not declare them. For a file-based research task, use checks=[{"kind":"tool","tool":"read_file","path":"brief.md"},{"kind":"artifact","path":"brief.md","format":"text","min_bytes":1}] with the actual requested file. File checks read actual bounded workspace bytes; command checks require a zero exit code from the exact observed command and never execute additional commands. Checks cover only declared predicates, not semantic correctness.
- Retrieve task_result before relying on a delegate. verified=true means the declared predicates passed; verified=null means no checks were requested: explicitly report that machine acceptance was not performed and never say those checks passed. Never call either a done status or a VERIFICATION heading independent acceptance. A spawned or done task is not proof that the user's goal was verified. Check the reported evidence, artifacts and blockers; disclose missing tools or failed checks.
- Specialized financial and warehouse capabilities remain optional domain extensions. Do not impose stock symbols, trading signals or financial-evidence requirements on unrelated tasks.
"""
