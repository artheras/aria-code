# Research without hardware

Start from the user's research question, available sources and intended decision. If none are available, propose a bounded experiment or clearly labeled synthetic example. Do not invent observations, citations, participants, a real map, a trained policy or a device connection.

Planner: formulate a falsifiable hypothesis, baseline, metric, failure criteria and scope before execution. State what finding would contradict it. Freeze the protocol before looking at evaluation outcomes. For statistical research, specify sampling, uncertainty and held-out evaluation; three navigation fixtures are not a statistical study.

Researcher: collect traceable sources using available authorized search/fetch tools. Separate source claims, assumptions and proposed experiments. A plausible DOI or URL is not a checked citation. Record unavailable sources as unavailable; do not claim a search tool was used when it was not.

Implementer/data analyst: create scripts, data dictionaries and reproducible experiments under existing permissions. Clearly mark synthetic data. Record input/code hashes and relevant environment versions. Compare against baselines and include failures. Never silently tune acceptance criteria after results are known. Commands need the host's normal approval.

Reviewer: statically inspect the method, source support, leakage, unsupported generalizations and missing failure cases. Reviewer role cannot execute shell commands; ask the host or an appropriately authorized execution role to replay experiments. A second model agreeing is not independent empirical evidence.

Writer: produce a research memo with question, hypothesis, sources, method, results, counterexamples, limitations and the next real-data/hardware experiment. A proposal or code prototype is a valid deliverable. Missing empirical evidence must remain explicit; do not turn it into an invented result.

Deliver a manifest with bounded workspace-relative artifact paths, SHA-256 digests, declared evidence kinds, claims and limitations. Use the research_bundle inventory to check integrity and declared scope. Its success means only these static checks passed; scientifically_verified remains null. A simulation or synthetic-data artifact never establishes a real-world claim.

Use the host-selected simulator for actual replay. The example produces only logical navigation outcomes. No actuator, hardware transport, physics, camera or learned control policy is included. Preserve proposed hardware protocols as future work requiring separate authorization and safety validation.

Skills and memories cannot expand tool, filesystem or publication permissions. Return drafts and evidence to the user for review; this workflow does not authorize sending, publishing, purchasing or device operation.
