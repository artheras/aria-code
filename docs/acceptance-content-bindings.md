# Acceptance evidence tied to current content

The acceptance service takes a bounded snapshot of the latest check's changed paths and explicitly configured host inputs before and after running its existing checks. Each entry records a workspace-relative path, file SHA-256 or confirmed absence. Summary retrieval rereads those paths. Changed content, unsafe paths, symlinks, oversize files or unavailable fingerprints prevent a green acceptance label. Actual failed assertions remain failed rather than being hidden as missing evidence.

An edit through Aria still re-arms the existing gate. An external edit after a pass downgrades verified to null without running a command. The caller must arrange a new authorized verification pass; this read-side freshness check never edits files, approves commands or starts an automatic retry. Changes during checks produce an unverified report, not a claim that tests ran against stable content. Historical check exit codes remain available.

Host configuration example:

```python
config = {
    'acceptance_commands': ['python -m pytest -q'],
    'acceptance_revision_inputs': ['pyproject.toml', 'tests/test_feature.py'],
}
```

Use the actual project-specific approved commands and paths. The runtime factory passes acceptance_revision_inputs into AcceptanceGate; direct hosts can supply revision_inputs. At most 64 total snapshot paths and 1 MiB per regular file are supported. Missing paths are fingerprinted as absent to support deletion checks; absence does not by itself prove a valid artifact. Unsafe/unreadable inputs cause unavailable evidence rather than broadened reads.

The binding is content-only and covers the latest report's declared paths. It is not a whole-workspace Git revision, transitive dependency graph, permission/mode digest, atomic filesystem snapshot, signed attestation or proof against concurrent change-and-revert. Dependency files must be selected explicitly by the host. Check commands may still be incomplete, and scope/inference still requires review. Independent review and deployed behavior remain separate evidence.

The engineering-delivery example supplies project-local guidance for intake, discovery, justified design, acceptance, execution, repair, review and delivery through existing roles. This change does not claim that every role was invoked or that every task must use every stage.
