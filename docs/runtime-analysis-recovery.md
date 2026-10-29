# Analysis recovery and task completion

This patch targets the v0.164.0 source tree (98a21c0cddbe1ed37878236259501e130f4382c5). It addresses financial-source diagnostics, misleading in-progress phases, and coding turns that repeatedly write completion reports.

Financial statements use a shared 20-second HTTP budget, not only a session timeout that yfinance overrides. Income and balance sheets must share a reporting date. Missing cells remain unknown, zero denominators do not become one, and the former incomplete Altman approximation no longer produces a solvency classification. Errors distinguish dependency, connectivity, timeout, empty data, and incompatible reporting dates. A failed financial lookup does not require Google login and should not prevent other evidence-backed analysis.

Coding turns retain the original request for system-prompt selection and tool scope. The prompt explicitly permits a final text answer after the work; a guard bounds consecutive identical successful writes. Only the verified Nemotron Super model is registered for native tool calling; unregistered NVIDIA models remain conservative.

The native frontend clears its active inspection phase after all current tools return, and labels outstanding calls as cancelled with their turn. This does not infer why a user cancelled from a screenshot. The package-qualified worker import and source test path avoid resolving an older top-level Python installation.

Local verification included Python runtime, native worker protocol, cancellation/resume and regression tests. A real Nebius NVIDIA task started from an empty workspace and finished in about 39 seconds with program generation, actual command execution, report read-back, successful configured acceptance (unit tests, CLI, external independent acceptance), and final success. Earlier attempts exposed a test that deleted the result artifact and insufficient model-written numeric tests; the final acceptance explicitly covers both. These checks are configured for that demonstration, not automatically inferred for every user task.

Live Yahoo checks on the test machine still returned no usable statements (AAPL timeout around 21 seconds; NVDA empty result around 16 seconds). This patch improves bounded failure and honest metrics; it does not claim Yahoo service availability has been restored. Native validation subsequently completed locally: 119 Rust tests passed, a new frontend was built, and its worker-protocol suite passed 32 tests (8 skipped). The combined Python regression passed 279 tests (8 skipped). The preview launcher uses this new frontend; the official installed entrypoint has not been replaced.

Run the focused Python regression with `python -m pytest tests/test_financial_statement_recovery.py tests/test_nebius_workspace_regression.py tests/test_model_capability_registry.py tests/test_runtime_agent_loop.py tests/test_native_app.py`. Build and test the native UI from `rust/` with `cargo test --locked -p aria-native`.

## Capability questions

Standalone questions such as “你除了分析股票外还能帮我做什么” describe the assistant, not a current market-analysis task. They now classify as general conversation, skip deterministic finance routing and financial evidence requirements, carry no project context or tools, and block unsolicited tool execution. Full-message matching preserves tools and evidence requirements for mixed requests such as “what can you do; analyze AAPL today”. The original Chinese question passed a real Nebius replay in about 7 seconds with one inference request, zero offered tools and zero tool calls. Tests also cover the question after a financial turn in the native worker protocol.
