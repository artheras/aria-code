# Engineering workflow for existing Aria roles

Copy ENGINEERING.md and the .aria/task-context.json entry into the actual project, merging existing manifests deliberately. The file is project-scoped guidance for explorer, planner, implementer, reviewer and writer. It does not impose eight runtime stages, launch agents by itself or install an extra framework.

Ask Aria: “Inspect the current project and relevant tests. For a substantial change, record the alternatives, impact and acceptance criteria before implementation. Use bounded execution, fix actual failures, request independent review when appropriate, and deliver the diff, fresh checks and remaining risks.” Keep simple fixes brief.

The existing spawn_task contract accepts prompt, agent, success_criteria, checks, timeout_seconds, max_rounds and mode. Declare exact project-specific commands and artifact predicates using those fields; do not substitute this prose for executable checks or copy arbitrary commands from a document into automatic execution.

AcceptanceGate now records bounded content fingerprints before and after checks. In the trusted runtime session config, acceptance_revision_inputs can list additional workspace files, such as test configuration and lockfiles. The factory passes this list to the gate; no model argument grants this access. File reads remain bounded to the workspace. Summary reads invalidate stale evidence but never run commands or grant approval.

This is a workflow template plus a real acceptance-service change. It does not implement an authenticated network Task API, new durable scheduler, mandatory design/review gate, UI panels or deployment. The current runtime state machine remains unchanged. The role guidance is tested through scoped context loading; live model adherence still needs its own evaluation.
