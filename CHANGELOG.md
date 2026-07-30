# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

Stable trains (`MAJOR.MINOR.x` branches) receive **bug fixes only** — see
[RELEASING.md](RELEASING.md) for the release model.

## [Unreleased] — on `main` (active churn; pin `1.0.x` for stability)

Transfer-engine rework, headed for the next major train once field-proven:

### Added
- **Async xcopy transfer** (`Cisco-IOS-XE-xcopy-rpc`), ledger-primary: the
  install engine runs the transfer as a uuid-keyed operation-ledger record
  (immune to the platform's ~600s blocking-RPC ceiling that kills classic
  copy on slow WANs); success is the engine's own verdict confirmed
  byte-exact (normally from the engine's package inventory — no filesystem
  walk), failure quotes the engine's failing transaction. Bench-validated
  end-to-end on 17.18.03 including ~15-minute transfers; WAN field runs
  pending.
- **Image transfer method** input: `Async xcopy (default — classic-copy
  fallback)` / `Classic copy only`. Fallback to classic copy happens up
  front on wire-proven preconditions (ported image URL, no recorded file
  size) or after a positively terminal device-reported xcopy failure —
  never on ambiguous ends.
- Walk-free, ledger-first pre-check and progress reads (SELinux AVC burst
  profile drops to typically one filesystem walk per run).

### Changed
- Classic copy is now the **fallback tier** (still selectable outright).

### Removed
- The **engine-download** experiment (`install add` from a remote URL) —
  bench-validated 2026-07-28, then removed: no remaining niche next to
  xcopy + the classic fallback. Findings retained in the README.

## [1.0.0] — 2026-07-26

First stable release. The **`1.0.x`** train is cut from this commit and will
receive bug fixes only; feature development continues on `main` toward the
next train.

### The release contains

- **Cisco IOS-XE Upgrade (RESTCONF)** — install-mode upgrades driven entirely
  over RESTCONF: staged run scopes (copy / copy + prep / full), engine-ledger
  tracking of `add`/`activate`/`commit`, byte-exact copy verification,
  engine-idle gating, boot verification before commit, and the platform's
  auto-rollback timer as the uncommitted-failure safety net.
- **Register IOS-XE Image** — registers images against Nautobot core
  `SoftwareVersion` / `SoftwareImageFile` records, with optional server-side
  checksum verification.
- **Cancel IOS-XE Upgrade Run** — cooperative, safe-boundary cancellation of
  a running upgrade with a per-device post-mortem.
- Opt-in extras on the upgrade job: clean device first, config saves (before
  reload / after commit), Golden Config backups (before & after the run),
  pre/post health checks (report-only), quiet-SELinux log filter, and remove
  inactive after commit.
- Documentation: phase-by-phase README with overview and detailed flow
  diagrams, image storage & registration guide, tiered roadmap.

### Validated at release

30+ lab upgrade/downgrade runs (Catalyst 9300, 9300L, C8000V; single
switches, a 2-member stack, serial and parallel batches; Nautobot 2.4 and
3.1), early production runs at two organizations (a 9500 StackWise Virtual
core; a three-stack site of 6–7-member stacks through the full staged cycle),
and one field-observed auto-rollback of an unconfirmable upgrade. See the
README's *Current status* for what is not yet proven.

[1.0.0]: https://github.com/bforejt/nautobot-upgrades/releases/tag/v1.0.0
