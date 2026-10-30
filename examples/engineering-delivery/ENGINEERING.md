# Engineering delivery workflow

Use the amount of process the task needs. A small reversible fix can use inspect → change → relevant checks → delivery. Architecture, authorization, migrations or multi-component changes need the fuller decision record below. Do not create mandatory agent stages or repeat approval requests for work already authorized by the user.

## Intake and discovery

State the user's goal, non-goals, actual project, known constraints and missing inputs. Inspect repository instructions, current interfaces, relevant code/tests and user changes before planning. Cite concrete file locations as evidence; label assumptions. Ask only when a missing answer blocks safe progress.

## Decision and acceptance before implementation

For a complex change, record the problem, observed facts, viable alternatives, chosen approach, affected interfaces/data/permissions, compatibility/migration risks and validation plan. This is an auditable engineering decision, not hidden chain-of-thought.

Declare acceptance predicates before implementing. Use the existing spawn_task checks contract for artifacts and exact approved commands; success_criteria prose is supplementary. Keep scope and budgets bounded. Do not silently relax a failed criterion. If a criterion must change, disclose why and preserve the prior outcome.

## Implementation and repair

Use the existing implementer role and host permission/worktree rules. Preserve user edits. Run relevant checks through normal tool policy. Reuse the existing acceptance repair loop: inspect actual failures, make a focused correction and rerun affected checks. Distinguish pre-existing failures, unavailable dependencies and newly introduced failures. Stop with explicit unresolved work when budget is exhausted.

After a passing check, changes to covered files invalidate its content binding. Configure acceptance_revision_inputs in the trusted host session for relevant test/config/lock files. These snapshots cover only declared paths; they do not certify all dependencies, Git state, external services or a running deployment.

## Review and delivery

Use a reviewer with separate task context for complex changes when available. Its static findings supplement tests. The reviewer role cannot execute commands; execution evidence must come from the host/authorized execution role. Do not claim independent review if only the implementer reread its answer, or if no reviewer ran.

Deliver: completed scope, changed artifacts/diff, actual checks and outcomes, evidence scope/freshness, review findings or review-not-performed, unresolved risks and user actions. done, code generated, a green old log or model prose alone is not verified delivery. Missing checks remain unverified. Do not claim deployment, publication, real-world validation or a client integration that was not actually exercised.

Planning cannot grant access or authorize publication. Follow the current host's approval policy. This project guidance does not add filesystem scope, execute hooks, modify permissions or approve commands.
