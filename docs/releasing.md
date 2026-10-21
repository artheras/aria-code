# Releasing

A release is a decision. Merging to `main` never publishes; **Actions → Release on merge →
Run workflow** does, and only from `main`.

## Before you start

- Everything you want shipped is merged and `main`'s checks have finished and are green.
  The workflow refuses to run while checks are pending or red.
- Decide whether this one is worth shipping now. Ship soon after a security fix. Otherwise
  batch a stable set of user-visible changes, roughly weekly, so the list does not grow to
  dozens of commits that are hard to reason about when something breaks.
- If the release changes how the app updates itself, how it is approved, or what it may do
  on a person's computer, say so in `note`. Those are the changes users cannot undo.

## Running it

| Input | Meaning |
|---|---|
| `confirm` | type `RELEASE`; anything else stops before a tag exists |
| `note` | optional first line of the changelog entry |
| `skip_checks` | release although main's checks are not green; use only for a known flaky job, and say which in the run |

The checklist job runs first and writes what will be released (every commit since the last
`v0.*` tag) into the run summary. It stops if nothing changed, if it was not started from
`main`, or if checks on `main`'s latest commit are pending or failed. Only then does the
existing pipeline run: bump, changelog (one line per commit), tag, binaries for every
platform, npm, PyPI, GitHub packages, and finally the draft GitHub Release is made public by
`finalize_release.sh` after it verifies the artifacts.

## Version numbers

Unchanged: major is `0`, minor counts releases, patch is for a fix on top of a shipped
release (`python scripts/bump_version.py --next-hotfix`, run by hand on purpose).

## What this does not give you yet

There is no preview channel. Every release is the one users install and, for managed native
installs, the one background auto-update fetches next (`auto_update.py` only distinguishes
*how* Aria was installed, not a stable/preview track). Until there is a track, soak a release
yourself before running the workflow: install from a built checkout or the previous release
plus the branch, and use it for a day.
