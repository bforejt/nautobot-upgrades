# Archived: RESTCONF Dev Tester

A retired **bench instrument**, kept here as working reference code —
[`restconf_dev_tester.py`](restconf_dev_tester.py) is the complete job as it
last ran, review-hardened and field-proven. It is NOT registered and NOT part
of the upgrader.

## What it was

An evidence recorder: point it at ONE device, and it captured raw RESTCONF
responses, HTTP statuses, and read timings as Job Result file artifacts, with
a UTC-stamped `00-manifest.json` ledger of every request. Built for the
Catalyst 9800 AP-predownload bench (2026-08), where it replaced a
seventeen-probe manual Postman sheet and produced the captures the 9800
upgrade job's design and scenario battery are built on. Retired once that
bench completed: its probe packs encoded that bench's specific questions, and
a future investigation needs new packs anyway.

## The design contract (the part worth stealing)

**The job RECORDS and never DECIDES.** Every rule below was either a review
finding or a field lesson — a reconstruction should keep all of them:

- GET suites strictly read-only; custom paths **positively validated**
  (`data/` prefix is not enough — dot-segments and `operations/` smuggling
  refused and ledgered).
- POST suites fire exactly ONE operator-selected RPC behind an
  acknowledgement checkbox tested by **identity** (`is not True` — stored
  ScheduledJob kwargs bypass form coercion and the string `"false"` is
  truthy). Record before/request/response/after; no retries, no chaining,
  no interpretation.
- An HTTP error is a RESULT, not a failure — record it verbatim. A
  transport failure must never produce a zero-byte artifact (attach a loud
  `{status, error}` stub instead) and an **unreadable poll is never
  evidence** (the watch's steady-stop counts only genuine 2xx reads;
  unreachable must not masquerade as steady).
- Every manifest row carries a UTC timestamp — unchanged polls included
  (wall-clock questions live on them).
- Bodies decoded as **UTF-8 per RFC 8040/7951**, never requests' charset
  guess; truncation counts encoded bytes; an aggregate artifact budget
  degrades a runaway watch to a manifest-only ledger instead of an OOM.
- `Meta.soft_time_limit` must contain the longest watch — stock Nautobot's
  300 s Celery default kills a 30-minute watch mid-sleep. Attach the
  manifest in a `SoftTimeLimitExceeded` handler so a killed run still
  yields its ledger.
- Advisory analyses are log-only and may never crash the recording.

## Reconstructing a bench instrument

1. The recording primitives are STILL LIVE in
   [`jobs/restconf.py`](../../../jobs/restconf.py): `probe_get` /
   `probe_post` — never-raising, status + elapsed + UTF-8 body + true byte
   count. Build on those; do not reinvent the transport.
2. Copy the archived file back to `jobs/`, register it in
   `jobs/__init__.py`, and replace the suite tables (`SNAPSHOT_PROBES`,
   `WATCH_PROBES`, `POST_SUITES`) with the new investigation's questions —
   that is the only part that was 9800-specific.
3. If it should exist only in dev environments, gate the registration on a
   worker env var (the pattern retired with this tool: conditional import in
   `jobs/__init__.py` keyed on e.g. `NAUTOBOT_UPGRADES_DEVTOOLS`, so
   production never imports the module at all).
4. Artifacts attach via `Job.create_file` (guarded — its absence or failure
   must never kill a run). Name them after the bench sheet's probe numbers
   so the sheet's results log stays the shared vocabulary.
5. Evidence hygiene: captures carry internal hostnames/IPs/serials. They
   belong in Job Results and private storage; the repo `.gitignore` refuses
   the common export shapes, and nothing under `docs/archive/` may ever
   include a real capture.

## What it proved before retiring

The 2026-08 bench arc it recorded: the predownload fire's 204-empty accept
and 4xx refusal shapes, World-2 semantics of `predownload-data`, the
vanishing-entry signature of an interrupted AP (the port-pull), the
backup-partition corroborator, post-activate auto-swap, the 6-hour
auto-abort default, `clear-ap-predownload-statistics` being unimplemented
over RESTCONF on 17.15.5, and the standalone-CL topology-string trap. Those
findings live on in `jobs/c9800_upgrade.py`'s gates, `jobs/constants.py`'s
provenance comments, and the scenario battery's fixtures.
