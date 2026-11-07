# GitHub Enterprise Migration Runbook

For current team development and service ownership, use
[GitHub-first development](github-first-development.md). This file records an
earlier organization migration, not the current source checkout procedure.

> **Historical.** This records the move into the `artherahq` organisation. That organisation has since been renamed to **`artheras`**; the canonical repository is now `https://github.com/artheras/aria-code`. Do not use the `artherahq` URLs below — GitHub only redirects them until someone else registers the old name.

This runbook moves Aria Code from a personal GitHub repository to an Arthera
organization repository without losing local history, open work, or deployment
secrets.

## Current State

At the time this runbook was added, the local repository used:

- canonical local path: `/Users/mac/Desktop/aria-code`
- personal remote: `origin -> https://github.com/artherahq/aria-code.git`
- active automation branch pattern: `codex/...`

The `codex/` branch prefix is a development namespace created by agent-assisted
work. It is safe as a temporary feature branch prefix, but it should not be the
long-term release branch namespace.

## Target Repository

Recommended target:

- GitHub organization: `Arthera`
- repository: `aria-code`
- canonical URL: `https://github.com/artherahq/aria-code`

Use `Arthera/arthera-code` only if the product is being renamed. If the CLI
continues to ship as `aria-code`, keep the repository name stable.

## Branch Model

Long-lived branches:

- `main`: protected production branch.
- `develop`: optional integration branch if releases need staging.

Short-lived branches:

- `feature/<topic>`
- `fix/<topic>`
- `refactor/<topic>`
- `chore/<topic>`
- `docs/<topic>`
- `release/vX.Y`
- `codex/<topic>` for temporary Codex-authored work only.

Do not merge directly to `main`. Use pull requests with CI passing.

## Required GitHub Settings

Enable on `main`:

- Require pull request before merge.
- Require status checks to pass.
- Require branches to be up to date before merge.
- Require linear history, if the team prefers squash/rebase merges.
- Restrict force pushes.
- Restrict deletions.
- Require secret scanning and push protection.

Recommended repository features:

- Issues: enabled.
- Discussions: optional.
- Projects: optional.
- Wiki: disabled unless there is a defined owner.

## Required Secrets

Do not copy local `.env` files. Add only required CI/deploy secrets to the
Arthera organization or repository secret store.

Common optional secrets:

- `ARIA_API_KEY`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `DEEPSEEK_API_KEY`
- `GEMINI_API_KEY`
- `FINNHUB_API_KEY`
- `BRAVE_SEARCH_API_KEY`
- `FRED_API_KEY`
- broker paper keys such as `ALPACA_API_KEY` and `ALPACA_API_SECRET`

Live broker credentials should not be CI secrets unless there is a dedicated,
audited deployment environment with manual approval gates.

## Local Migration Steps

Run the preflight first:

```bash
python3 scripts/github_migration_preflight.py
```

Add the Arthera remote without replacing `origin`:

```bash
git remote add artherahq https://github.com/artherahq/aria-code.git
git fetch arthera
```

Push the current branch for review:

```bash
git push -u arthera HEAD:feature/runtime-sdk-migration
```

After review, create a pull request into `main` or `develop` in the Arthera
repository.

Only after the Arthera repository is verified as canonical:

```bash
git remote rename origin personal
git remote rename arthera origin
git remote -v
```

## Cutover Checklist

- [ ] Current local work is committed.
- [ ] CI passes on the Arthera repository.
- [ ] Branch protection is enabled.
- [ ] Secret scanning and push protection are enabled.
- [ ] Release tags are visible in the Arthera repository.
- [ ] README, package URLs, install commands, and issue links point to Arthera.
- [ ] Local `origin` points to the Arthera repository.
- [ ] Personal repository is archived or marked as moved.

## Rollback

If CI, permissions, or secrets are not ready:

```bash
git remote set-url origin https://github.com/artherahq/aria-code.git
git fetch origin
```

Do not delete the personal remote until at least one release has been cut from
the Arthera repository.
