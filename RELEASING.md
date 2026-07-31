# Releasing

How this repository turns "works on `main` today" into something production
can depend on. The short version: **production tracks a train branch; a train
only ever receives bug fixes.**

## The model: stable trains

- **`main` is development.** It moves freely and may change behavior between
  commits. Point labs and evaluations at it — never production.
- **`MAJOR.MINOR.x` branches** (e.g. `1.0.x`) are **stable trains**. A train
  changes **only to fix bugs** — no new features, no changed defaults, no
  renamed inputs. Nautobot pins Git repositories by *branch*, so production
  points at a train branch and can re-sync at any time without surprises.
- **Moving to a newer train is always the operator's deliberate act** (edit
  the Git Repository's branch field). A train never turns into a different
  train underneath its users.

## Versioning (SemVer)

- **PATCH** (`1.0.1`) — bug fixes only. The only thing a train receives.
- **MINOR** (`1.1.0`) — new features or opt-in inputs, backwards-compatible.
  Ships as a **new train branch**.
- **MAJOR** (`2.0.0`) — anything that changes existing inputs, defaults, or
  behavior an operator may depend on. Ships as a **new train branch**.

Every release is tagged `vX.Y.Z`. `JOB_VERSION` in
[`jobs/constants.py`](jobs/constants.py) (exported as `jobs.__version__`) is
logged by the upgrade job at the start of every run, so each Job Result
records exactly which code produced it:

- **On a train branch** it must match the tag exactly.
- **On `main`** it carries the next train's version with a **`-dev`**
  suffix (e.g. `2.0.0-dev`), so a development run can never be mistaken for
  a stable release in the log. A bare `X.Y.Z` on `main` is always a bug —
  set the `-dev` value in the same commit that cuts a train.

## Support policy

- **One active train.** Fixes are cherry-picked to the newest train only;
  when a new train is cut, the previous train gets at most a short grace
  period for critical fixes, then is closed.
- **Fixes are `main`-first.** Fix on `main` (normal PR + review), then
  `git cherry-pick -x` to the train. A train never carries logic that `main`
  lacks.

## Cutting a patch release (on a train)

1. Land the fix on `main` (PR, review, merge).
2. `git cherry-pick -x` the fix commit(s) onto the train branch.
3. On the train: bump `JOB_VERSION`, add the CHANGELOG entry (mirror the
   entry on `main` too).
4. Verify on the train tip: CI green (ruff lint, ruff format check,
   byte-compile) and a **Dry-run against real hardware**; when the fix
   touches upgrade logic, a live lab run on an affected platform.
5. Tag `vX.Y.Z` on the train tip; push the branch and tag; publish a GitHub
   Release with the CHANGELOG entry.

## Cutting a new train (minor or major)

1. On `main`: bump `JOB_VERSION`, write the CHANGELOG section, merge.
2. Branch `X.Y.x` from that commit; tag `vX.Y.0`; push branch and tag.
3. Publish the GitHub Release from the tag, notes from the CHANGELOG.
4. Update the README's **Releases & pinning** section to recommend the new
   train, and announce the previous train's grace period.
