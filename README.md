# nautobot-upgrades

A native **Nautobot Job** that reliably and cautiously upgrades **Cisco IOS-XE**
devices — **Catalyst 9300** primarily — driven entirely over **RESTCONF**.

## Current status: lab-proven

**Thoroughly exercised on real Catalyst 9300 hardware, entirely over RESTCONF —
but not yet production-vetted.** It is a working prototype under active
development: expect change between releases, read the Job Result logs, and
**always run Dry-run first**.

**Validated on real Catalyst 9300 hardware** (from Nautobot 3.1):

- **Full upgrade _and_ downgrade** on **single switches**, repeatedly, across
  **17.12 → 17.15 ↔ 17.18 ↔ 26.1**.
- **Lettered rebuilds** as distinct versions (17.15.4 ↔ 17.15.4d), up and down.
- **Serial batches**, including a batch downgrade and correct already-on-target
  short-circuits.
- **Parallel batches at Parallelism 2** — run repeatedly (10+ times across
  various versions); the per-device isolation (own RESTCONF sessions, own
  ledger uuids) holds up in practice.
- **2-member stack**, up and down the **17.12 → 17.15** boundary (17.12.4 →
  17.15.5 → 17.15.4): per-member free-space gating, package distribution, and
  the all-members-rejoined gate.
- **Ledger-tracked** add/activate/commit, engine-idle gating, byte-exact copy
  verification, auto-rollback-timer arming, remove-inactive, and interrupted-run
  (commit-to-be-safe) recovery.
- Installs and runs as a Git Repository job on **Nautobot 2.4 and 3.1**; a full
  26.1.1 → 17.18.3 device install ran from a stock 2.4.36.

**Not yet proven — treat as experimental:**

- **Parallelism above 2** — validated at 2 concurrent; larger fan-out (up to
  16) and firmware-server contention at scale are not yet stress-tested.
- **The timed pre-staging cycle** — staging itself runs on hardware, but the
  full stage-ahead → window-run timing has not been measured.
- **Stack reload on 17.18 / 26.1** — the stack has only been reloaded across the
  17.12 → 17.15 boundary so far; **stacks larger than 2 members** are also
  untested.
- **9300L/LM/X**, **C8000V**, and **9200 / 9400–9600** — identical image, flow,
  and models on paper, hardware runs pending; **17.9–17.11**.
- **Failure paths on hardware**: auto-rollback expiry, a genuinely corrupt image,
  a member failing to rejoin.

Per-train and per-platform detail is in [Versions & support](#versions--support).

---

## Background & intended use

This project was built for a specific, common situation — and it is still a
**prototype**:

- **The fleet is uniform Cisco Catalyst 9300s.** A switching estate
  standardized on one platform and one image family, where an upgrade playbook
  that handles the 9300 well handles most of the network.
- **The inventory already lives in Nautobot.** Devices, platforms, primary
  IPs, and credentials (**Secrets**) are populated and maintained in a working
  Nautobot — so the source of truth for *what to upgrade* and *how to reach it*
  is already there, and this Job simply reads from it.
- **The team values REST.** Operators comfortable with REST and what it buys —
  structured request/response, idempotency, and true device state instead of
  screen-scraping CLI — rather than a traditional SSH/TFTP-driven upgrade.

It began as a **research question**: how much of an IOS-XE install-mode upgrade
could be driven *purely* over RESTCONF, with no CLI and no SNMP? On the code
trains this fleet runs, the answer turned out to be **essentially all of it** —
image copy, `install add`/`activate`/`commit`, reload, rollback, and the state
reads that gate each step. That result was strong enough to justify building
this prototype rather than stopping at a feasibility note.

**Where it stands:** the flow is working well and is thoroughly exercised in a
**lab** on real Catalyst 9300 hardware (see [Current status](#current-status-lab-proven)).
The intended next step is deliberate **production vetting** — the design is
conservative and its stability so far is encouraging, but "works in the lab" is
not "proven in production," so every run should still start with **Dry-run**.

**Key design choices** (from an up-front analysis, to avoid reinvention):

- **RESTCONF drives the entire upgrade** on modern IOS-XE — the
  `Cisco-IOS-XE-install-rpc` model (`install`/`activate`/`commit`/`remove`) plus
  the classic `Cisco-IOS-XE-rpc:copy`. The floor is **17.9.1**, the lowest
  model-complete release; older is refused. (The async `xcopy` was tried and
  abandoned — a real 17.15.05 silently broke it.)
- **Integrity without the on-device `verify` RPC**: optional server-side
  **hash-verify** at registration, a **byte-exact size match** after every copy,
  and `install add`'s **mandatory signature validation** before activation —
  stronger than an MD5 self-check because it catches tampering too. (The native
  `verify` RPC exists but returns only async event notifications with no pollable
  result, so it isn't used; it becomes a clean addition if Cisco ever makes the
  result pollable.)
- **Reuses Nautobot core, adds no data models of its own**: `dcim.SoftwareVersion`
  and `dcim.SoftwareImageFile` (core since Nautobot 2.2) already hold the image
  name, checksum, size, download URL, and device-type map.
- **Shipped as a Git Repository, not a packaged app** — the idiomatic way to
  deliver jobs from public GitHub. Its one constraint (git-delivered jobs can't
  install their own pip dependencies) is a non-issue here: the only dependency is
  **`requests`**, always present with Nautobot core.

## What it does

From the Nautobot **Jobs** page you scope target devices — filtering by
**location, role, status, platform, device type, current version, and tags** —
pick a target version, and the job runs an **install-mode** upgrade as a series
of PASS/FAIL gates, stopping at the first failure for a device. In one picture:

[![IOS-XE upgrade — high-level overview](docs/overview-flow.svg)](docs/overview-flow.md)

The six phases (the numbered keys on the diagram above map to this list):

1. **Connect** — resolve the primary IP + credentials (from core Secrets),
   confirm RESTCONF is reachable.
2. **Pre-flight gates** — running version and already-on-target short-circuit;
   **≥ 17.9.1**; **install mode**; image resolved from Nautobot with device-type
   compatibility; **enough free space**.
3. **Copy + verify** — the device pulls the image (classic `copy` RPC, watched
   for live progress), gated on a **byte-exact size match** and backed by
   `install add` signature validation. Skipped if the file is already on flash.
4. **Install** — `install add` → **activate** (explicitly non-ISSU, by the
   device's full internal version; a silently-dropped activate is detected via
   the ledger and re-sent) → **reload**. Every engine write is **gated on
   engine-idle and tracked to true completion in the device's operation
   ledger** — never trusting the RPC's 2xx; a ledger-recorded failure aborts
   quoting the engine's own failing phase. (Where a release doesn't populate
   those signals, the job degrades to version-state inference and a settle
   timer, labeled as fallbacks in the logs.)
5. **Verify, then commit** — reconnect, confirm the target actually booted, and
   **only then** `install commit`. If it didn't come back or booted wrong, the
   job does **not** commit and the device auto-rolls-back.
6. **Sync + optional cleanup** — update `Device.software_version` in Nautobot;
   optionally `install remove inactive` to reclaim space (off by default).

Every gate logs to the Job Result with the device attached (a **Debug** toggle
logs every RESTCONF call). Batches run **in parallel**
([details](#parallel-batches)); a per-device failure doesn't stop the batch, but
**any failure marks the whole Job Result FAILED**. Per-device durations and the
reload outage window are logged for change-window planning.

See the **[full gate-by-gate decision logic](docs/upgrade-flow.md)** for every
gate and abort.

## Versions & support

| Component | Supported | Notes |
| --- | --- | --- |
| **Nautobot** | **2.4 LTM** and **3.1+** | Job execution verified on **3.1 and multiple independent 2.4 environments** (most volume on 3.1). **3.0 is untested by choice** — unmaintained since 3.1 shipped. Earlier 2.x (≥ 2.2) *may* work but is untested. |
| **Device OS** | Cisco IOS-XE **≥ 17.9.1** (incl. 26.x) | Hardware-validated across **17.12–26.1**; every YANG model the job touches verified against Cisco's published models 17.9.1–26.1.1. Model presence ≠ runtime behavior — do one supervised run per new train. Rebuild letters (17.15.4**d**) are **distinct versions**. |
| **Platform** | Catalyst **9300 family** + **C8000V** | 9300 hardware-tested; **9300L/LM/X** run the identical cat9k image and flow (run pending). **C8000V** (autonomous): all models verified, `bootflash:` discovered from the device (run pending). **9200** and **9400/9500/9600**: model sets identical (runs pending). **9800 WLC**: mechanically compatible but **operationally out of scope** — controller only, no AP predownload; a full-scope run is warned in-job. Nexus/NX-OS is a different API — not supported. **3650/3850 cannot be supported** (their terminal 16.12 train lacks the install API; Cisco's replacement, the 9300L, is supported). |

**By IOS-XE train:**

| Train | Status | Basis |
| --- | --- | --- |
| **17.12 / 17.15 / 17.18 / 26.1** | ✅ **Tested on real equipment** | Repeated upgrades **and** downgrades on 9300s — single switches across all four trains, plus a 2-member stack across the 17.12 → 17.15 boundary; lettered rebuilds; cross-era moves in both directions; serial batches. Ledger tracking, engine-idle gating, byte-exact verify, remove-inactive, and interrupted-run recovery exercised live; the member-rejoin gate on the stack run. |
| **17.9 / 17.10 / 17.11** | ⚠️ **Not tested — might work** | Model-complete on paper (17.9 is the floor). Best used as an *escape source* (upgrade FROM it) — 17.9 left Cisco maintenance Aug 2025. Run one supervised upgrade first. |
| **< 17.9** | 🚫 **Not supported** | Refused: key API components are missing below the floor (the RESTCONF install models and reliable file-size reporting the job relies on). |

The job imports only **`requests`** plus Nautobot core, so there is no separate
Python dependency matrix — whatever ships with a supported Nautobot suffices.

## Authentication

Every device is contacted with credentials — nothing is attempted anonymously.
Credentials are resolved **at run time from Nautobot's Secrets manager**, never
typed into the job and never stored in job-run records (`has_sensitive_variables`
stays effective because no secret is a job input).

How it resolves, per device:

1. The job uses the device's assigned **Secrets Group** (`Device.secrets_group`),
   or the optional **Secrets group** job-input override.
2. It reads the **username** and **password** secrets, trying access types in
   order **RESTCONF → HTTP(S) → REST → Generic** (store them under **RESTCONF**).
3. They are sent as **HTTP Basic auth over HTTPS** — the mechanism IOS-XE
   RESTCONF uses (backed by the device's AAA: local / TACACS+ / RADIUS).

Because Nautobot Secrets are **provider-agnostic**, the secret values themselves
can live in environment variables, files, or an external manager (HashiCorp
Vault, AWS Secrets Manager, Azure Key Vault, Delinea, …) via the corresponding
Nautobot secrets-provider app — the job calls `get_secret_value()` and is
indifferent to the backend.

**Setup:** create a Secret for the username and one for the password → add both
to a **Secrets Group** under access type **RESTCONF** (secret types *username*
and *password*) → assign the group to each device (or pass it as the override).
The account must be **privilege 15** / authorized for `install` and `copy`.

The pre-flight check distinguishes the failure modes so the Job Result is
actionable: **HTTP 401** → bad/missing credentials; **HTTP 403** → authenticated
but under-privileged (needs privilege 15); otherwise → connectivity / RESTCONF
not enabled.

## Image storage

The `.bin` images are **not** stored in Nautobot — Nautobot holds only the
metadata (`SoftwareImageFile`: name, checksum, size, `download_url`, device-type
map). The binaries are hosted by the companion **`nautobot-composer` stack's
`firmware` profile**: a **Filebrowser** UI (`:8088`, authenticated) for engineers
to upload, and a read-only **nginx `firmware-download`** service (`:9443` HTTPS /
`:9080` HTTP, network/ACL-restricted) that the **devices pull from** via the
RESTCONF `copy` RPC.

The **Register IOS-XE Image** job builds the device `download_url` from a
configurable base + the uploaded filename, validates the image is reachable
(preferring the worker's internal route to `firmware-download`, falling back to
the device URL), optionally downloads + hash-verifies it, and records the
`SoftwareImageFile` mapped to the compatible device types — creating the
`SoftwareVersion` too if you don't pick an existing one. It does not upload
files — publish via Filebrowser first.

Configure on the Nautobot worker: `FIRMWARE_BASE_URL` (device-facing base,
plain HTTP by default — e.g. `http://<host>:9080/images/` — because device TLS
clients reject the firmware server's self-signed cert), `FIRMWARE_BASE_URL_HTTPS`
(the HTTPS variant, stored instead when the job's **Use HTTPS URL** option is
ticked), and `FIRMWARE_INTERNAL_URL` (worker validation, default
`http://firmware-download/images/`). The base is overridable per run.

See **[docs/image-storage.md](docs/image-storage.md)** for the full design: URL
formats, the acquire → upload → register workflow, TLS notes, and retention.

## Installing into Nautobot (getting started)

This project is consumed the standard Nautobot way — as a **Git Repository that
provides Jobs**. Nautobot clones the repo, discovers the Jobs in
[`jobs/`](jobs/), and runs them on its own Celery worker; there is nothing to
`pip install`. The mechanics of Git data sources and Jobs are core Nautobot
features maintained by Network to Code — this section covers the
project-specific basics as bullets and links out to NTC's documentation for the
detailed steps.

**Prerequisites**

- A working **Nautobot 2.4 or 3.1+** (see [Versions & support](#versions--support)).
  Don't have one? The same author's
  [nautobot-composer](https://github.com/bforejt/nautobot-composer) is a
  Docker-Compose stack that ships a matching Nautobot **and** the firmware
  server this job pulls images from.
- **Inventory in Nautobot**: each target device needs a **primary IPv4**
  reachable from the worker, a **device type** mapped to the target version's
  **Software Image File** (or a default image on the version), and an assigned
  **Secrets Group** exposing a username + password under the **RESTCONF** access
  type (see [Authentication](#authentication)).
- A **Software Version** record for the target with a **Software Image File**
  carrying at least a **download URL** and **image file name** (add the **file
  size** to enable the post-copy size gate). The device must be able to reach
  that URL over a transport it supports (https/http/scp/ftp/tftp); embed
  credentials in the URL if the host requires them. Binaries live on the
  firmware server, not in Nautobot — see [Image storage](#image-storage).
- **Devices**: Cisco IOS-XE **≥ 17.9.1**, booted in **install mode**
  (`flash:packages.conf`), with **RESTCONF enabled** (`restconf` +
  `ip http secure-server`) and a **privilege-15** account (or exec-authorized
  for `install`/`copy`). Enabling RESTCONF where it is absent is out of scope.
- No extra Python packages: the Job's only runtime dependency is `requests`,
  already present with Nautobot core.

**Steps** (the basics — follow the linked NTC docs for the full how-to)

- **Add the repository.** In Nautobot, go to **Extensibility → Git Repositories
  → Add**, set the remote URL to this public repo, pick a branch, tick
  **Provides: Jobs**, and **Sync**. Getting the URL into the right place and the
  sync options are walked through in NTC's
  [Git as a Data Source](https://docs.nautobot.com/projects/core/en/stable/user-guide/feature-guides/git-data-source/)
  guide (and the
  [Git Repositories](https://docs.nautobot.com/projects/core/en/stable/user-guide/platform-functionality/gitrepository/)
  reference).
- **Enable the Jobs.** Newly synced Jobs are **disabled** by default. Under
  **Jobs → Jobs**, in the **IOS-XE Upgrades** group, edit and **Enable** each of
  *Cisco IOS-XE Upgrade (RESTCONF)*, *Register IOS-XE Image*, and *Cancel IOS-XE
  Upgrade Run*. How enabling works is documented in NTC's
  [Managing Jobs](https://docs.nautobot.com/projects/core/en/stable/user-guide/platform-functionality/jobs/managing-jobs/).
- **Know how Jobs run.** Jobs execute on Nautobot's Celery worker and log to a
  **Job Result**; permissions, scheduling, and the run model are core Nautobot
  behavior, covered in NTC's
  [Jobs](https://docs.nautobot.com/projects/core/en/stable/user-guide/platform-functionality/jobs/)
  guide.
- **After changing Job code**, re-sync the repository; on non-container installs,
  restart the Celery worker so the new code is loaded.

Then head to [Running it](#running-it) for the first (Dry-run) execution.

## Running it

1. Populate the target **Software Version** + **Software Image File** in Nautobot
   (download URL, image file name, and ideally checksum + size), and map the
   image to the relevant **device type(s)**.
2. Open the job, optionally narrow the list with the **location / role / status /
   platform / device type / current version / tags** filters, select **devices**
   and the **target version**, leave **Dry-run** checked (the default), and run
   it. Dry-run executes every read-only gate and reports exactly what *would*
   happen.
3. When the dry-run is clean, run it again with Dry-run unchecked.

**Expected device log noise during an upgrade** (benign — do not stop on these):
`%ISSU-3-ISSU_COMP_CHECK_FAILED` appears on every `install add` (the engine
auto-probes for a hitless ISSU path that Catalyst 9300s in normal deployments
don't have; our upgrade is reload-based by design), and affected releases emit
SELinux `%SELINUX-1-VIOLATION` AVC-denial bursts whenever ANY process asks `smand`
for a filesystem listing — including this job's own file reads (copy
pre-check, progress polls, transfer verify — see
[SELinux AVC log events](#selinux-avc-log-events-cause-and-workaround)
for the cause and the workaround). The repeated `%DMI-5-AUTH_PASSED`
entries are this job's own RESTCONF polling.

### Parallel batches

Batch runs upgrade up to **Parallelism** devices concurrently (default **4**,
range 1–16; `1` = strictly one at a time). An upgrade is ~90 % waiting — copy,
install, reload — so parallelism collapses batch wall-clock dramatically: a
12-device batch at parallelism 4 is ~3 waves ≈ 90 minutes instead of ~6 hours
serial. Each device's result line carries its own `[total: …]` for the
change-window arithmetic.

**Validation to date is at Parallelism 2** (run 10+ times across versions in
the lab). The per-device independence below is by construction, so higher
fan-out is expected to behave — but treat anything above 2 as unproven: raise
it deliberately and watch the first runs (see
[Current status](#current-status-lab-proven)).

**Why it's safe**: every device is fully independent by construction — its own
RESTCONF sessions, its own per-operation correlation uuids in the device's
install ledger, its own gates and timers. Nothing is shared between device
threads except the read-only job inputs.

**Sizing Parallelism**: the practical limit is the firmware server's capacity
for simultaneous image pulls (each device downloads the full image during its
copy phase) and log readability. 4 is a comfortable default for the bundled
nginx firmware server; raise it after watching a batch's copy-progress lines
for signs of contention (all devices' transfer rates dropping together).

**Reading the logs**: per-device entries interleave in **time order**, each
still attributed to its device — use the Job Result's per-object filtering to
read one device's story in isolation. The final per-device results table and
the success/failure verdict are unchanged: **green still means every device
succeeded**, and any failure marks the whole Job Result FAILED with winners
and losers named.

**If the job's time budget expires mid-batch** (soft time limit, default
2 hours): in-flight devices are **stopped at safe step boundaries** — between
steps, never mid-decision — within about one poll interval; queued devices are
cancelled; and the post-mortem names three lists: completed, stopped/failed
(each entry carries its reason), and never started. Everything is safe to
re-run — the idempotent gates (copy/add skip-if-done, commit-to-be-safe) pick
each device up where it stopped.

### Cancelling a run

Nautobot core has no cancel button for running jobs
([nautobot#2088](https://github.com/nautobot/nautobot/issues/2088)), so this
repo ships one as a job: **Cancel IOS-XE Upgrade Run**. Pick the running Job
Result and run it — the upgrade run receives the same signal as the soft time
limit, which it handles **gracefully by design**: every in-flight device stops
at its next safe step boundary (never mid-decision, within ~one poll
interval), queued devices never start, and the cancelled run logs the full
**completed / stopped / never-started** post-mortem. Stopped devices are left
at safe boundaries — re-running the upgrade job later picks each one up
(idempotent gates + commit-to-be-safe). Cancelling a *queued* run simply
prevents it from starting.

### Pre-staging (stage now, activate in the window)

An install-mode upgrade splits into a **harmless half** (copy the image;
`install add` extracts, distributes to every stack member, and marks the
version for activation — no reload, no boot change, nothing armed, a
Cisco-supported resting state that survives power cycles) and the
**disruptive half** (activate → reload → commit). The **Run scope** input
lets you do the harmless half ahead of time:

- **`stage-add`** (recommended): every pre-flight gate + copy + a
  ledger-confirmed `install add`, then stop. The maintenance-window run
  (scope `full`) skips the finished work automatically — the idempotent
  gates recognize it — and needs only **activate → reload → commit**,
  collapsing per-device window time to roughly the reload (~10–15 min).
- **`stage-copy`**: stop after the size-verified copy — for fleets tight on
  flash (staged packages roughly double the image's footprint until the
  window).

Staging causes **no outage** (it structurally can't reach `activate`), so it is
the safe scope to run during business hours and to push **Parallelism** higher —
with the same "validated at 2, raise deliberately" caveat as any batch (see
[Parallel batches](#parallel-batches)) — and it pairs naturally with Nautobot's
native job scheduling ("stage the fleet overnight"). Structural guarantee: stage scopes return
before any code path that can reach `activate` — the only disruptive verb.
If plans change, a staged image is inert; `install remove inactive` (or the
Remove-inactive option on a later run) reclaims the space.

**Clean-then-stage** for tight-flash devices (4 GB 9200s, 8 GB C8000V
profiles): tick *Clean device first* together with a stage scope — the device
is groomed by the install engine, the free-space gate evaluates the cleaned
flash, and the staged image lands with maximum headroom.

**The safe step is the default**: Run scope defaults to *Step 1 - Copy
image*, so an actual upgrade requires **two deliberate acts** — unchecking
Dry-run *and* selecting *Full* — and a forgotten dropdown can never reload a
device (the run just stages and says so). Anyone automating runs via the API
should pass `run_scope` explicitly.

### Cleaning a device first

The **Clean device first** checkbox tells the job to groom the device
*before* upgrading: it runs the install engine's own `install remove
inactive`, which deletes every piece of software the device is not
currently running — inactive packages, leftover image files, **and any
version another engineer may have staged**.

⚠️ **What you are accepting when you tick it:**

- **Anything in-flight is deleted.** A staged version usually means someone
  else's change is already underway. Normally the job STOPS when it finds a
  conflicting staged version (the staged-conflict safety stop); this
  checkbox is the deliberate override. Tick it only when you know the state
  of the network and nothing else is planned for this device.
- **It does NOT remove the rollback image for THIS upgrade.** The currently
  running version is active software, which `install remove inactive`
  cannot touch — and that is exactly what becomes the rollback image once
  the new version activates. What the clean deletes is one generation
  older: the version kept on flash from a *previous* upgrade. If that
  earlier upgrade is still in its soak window, cleaning removes its
  rollback option (going back that far would mean re-running this job
  targeting that version — a full re-copy).

The setting that DOES remove this upgrade's rollback image is **Remove
inactive (after commit)**: once the new version is committed and running,
the replaced version becomes inactive, and that option reclaims its space
right away instead of keeping it for a soak period (default off).

Mechanics: the clean runs before the free-space gate, so the gate evaluates
the CLEANED flash (this is the **clean-then-stage** pattern for tight-flash
devices described above). Clean failures abort the device's run; a dry-run
only reports what would be removed.

### Saving running-config before the reload (Full runs)

The CLI `reload` asks *"System configuration has been modified. Save?"* —
**RPC-triggered reloads never do.** The reload our activation triggers simply
discards unsaved running-config changes (Cisco's own model says as much: the
reload RPC's `force` leaf is described as *"Force a restart even if there is
unsaved config"*).

The job **cannot detect** whether a save is needed: the only
programmatically-readable source for the saved/unsaved determination is the
config-management timestamps served through the device's **SNMP bridge**
(`CISCO-CONFIG-MAN-MIB`), which requires an `snmp-server` configuration and
simply hangs without one — a dependency this project deliberately does not
take (verified on real hardware; no native YANG replacement exists even on
26.1). Detection was therefore removed.

What the job does instead:

- Tick **Save running-config before reload** and the job performs the save
  itself (`cisco-ia:save-config`, the programmatic `write memory` — a native
  DMI RPC with no SNMP dependency) right before activation. A refused or
  failed save **aborts before the reload**; success is confirmed by the
  device's own result string. Default **off**: saving is itself a write, and
  it would persist half-applied changes an engineer deliberately left
  unsaved.
- With the box unticked, Full runs log a one-line reminder of the platform
  fact before activating, so the silent-discard behavior is never a surprise.

### ISSU on Catalyst 9500/9400/9600 pairs (proposed workflow — untested)

This job's activation is **deliberately non-ISSU**: on a StackWise Virtual
pair or dual-supervisor chassis, a full-scope run reloads **everything at
once** — the documented default upgrade path, but exactly the total outage
that core sites use ISSU to avoid. Until a native ISSU mode exists, the
**proposed** pattern lets the job do everything around the one disruptive
verb, which stays human:

1. **Stage with the job** — Run scope *Steps 1 & 2*: every pre-flight gate,
   the verified copy, and a ledger-confirmed `install add` on the pair
   (non-disruptive; packages distribute to both chassis). Safe in business
   hours, batchable across core sites.
2. **Perform the ISSU by hand** in the window: `install activate issu` from
   the CLI, watched by an engineer — the rolling standby-first sequence,
   switchover, and ISSU eligibility (the engine's own compatibility check)
   remain under direct human control.
3. **Re-run the job** (scope *Full*, same target): the already-on-target
   path runs **commit-to-be-safe** (cancelling any pending rollback) and
   syncs Nautobot — the interrupted-run recovery flow, hardware-validated.

> **Status: proposed, never tested.** No part of this pattern has run
> against a real SVL pair or dual-sup chassis from this job. If we ever test
> it, this project will be updated accordingly — including a native ISSU
> mode (the activate RPC already carries the `issu` flag we set to false; a
> research spike on a lab pair would define the confirmation choreography).

### Job inputs

| Input | Required | Purpose |
| --- | --- | --- |
| Location / Role / Status / Platform / Device type / Current version / Tags | no | Optional filters that narrow the **Devices** picker for field operations. |
| Devices | yes | Target devices to upgrade (narrowed by the filters above). |
| Target version | yes | Core `SoftwareVersion` to upgrade to. |
| Clean device first | no | ⚠️ **Default off.** Before upgrading, remove ALL software the device is not running — including **any version another engineer staged** (overrides the staged-conflict stop). See [Cleaning a device first](#cleaning-a-device-first). |
| Run scope | no | Order of operations, safest first: **Step 1 - Copy image** (**default** — a forgotten dropdown can never reload a device), **Steps 1 & 2 - Copy image and prep** (`install add`, no reload), **Full - Copy, Activate, Reload** (the only choice that reloads; a real upgrade requires selecting it deliberately). See [Pre-staging](#pre-staging-stage-now-activate-in-the-window). |
| Save running-config before reload | no | **Default off.** RPC reloads never prompt to save, and the job cannot detect whether a save is needed (SNMP-only source — dependency declined). This box makes the job save (`cisco-ia:save-config`) before activating, aborting if the save is refused or fails. See [Saving running-config](#saving-running-config-before-the-reload-full-runs). |
| Quiet SELinux log noise on terminals | no | **Default off.** The harmless SELinux AVC-denial messages come from how the job watches files during an upgrade; enable this if you watch the **physical console or terminal-monitor (SSH)** and want them quieted there. `show logging` and syslog servers still record everything. Applied to the RUNNING config at the start of the run (every release); unsaved — erased by the reload — unless combined with *Save running-config before reload* on a **Full** run. See [SELinux AVC log events](#selinux-avc-log-events-cause-and-workaround). |
| Secrets group override | no | Force one Secrets Group for the whole run; by default each device uses its own assigned group. |
| Remove inactive | no | After commit, reclaim space (default **off** — keeps the rollback image for a soak period). |
| Parallelism | no | Devices upgraded concurrently (default **4**, max 16; 1 = serial). **Hardware-validated at 2 so far**; higher fan-out is unproven. Size to the firmware server's capacity for simultaneous image pulls. |
| Debug | no | Verbose RESTCONF request/response logging. |
| Dry-run | — | Read-only pre-flight only (default **on**). |

### RESTCONF operations used

| Step | RESTCONF call |
| --- | --- |
| Read version | `GET .../Cisco-IOS-XE-device-hardware-oper:device-hardware-data/device-hardware/device-system-data` |
| Stack member roster | `GET .../Cisco-IOS-XE-device-hardware-oper:device-hardware-data/device-hardware/device-inventory` |
| Install state / mode / ledger | `GET .../Cisco-IOS-XE-install-oper:install-oper-data` |
| Partition stats (discovery + space gate) | `GET .../q-filesystem?fields=fru;slot;bay;chassis;partitions(name;total-size;used-size)` |
| Full file listing (copy pre-check, per-poll progress, transfer verify) | `GET .../Cisco-IOS-XE-platform-software-oper:cisco-platform-software/q-filesystem` |
| Copy image | `POST .../operations/Cisco-IOS-XE-rpc:copy` (worker thread) |
| Add / activate / commit / remove | `POST .../operations/Cisco-IOS-XE-install-rpc:{install,activate,install-commit,remove}` |
| Save running-config (opt-in) | `POST .../operations/cisco-ia:save-config` |
| AVC suppression filter (opt-in) | `GET`/`PATCH .../data/Cisco-IOS-XE-native:native/logging` (read-before-write; merge only) |

## Configuration

Release- and site-specific knobs live in [`jobs/constants.py`](jobs/constants.py):
the version floor, target filesystem (`flash:`) and its **partition-name match**
(`TARGET_FS_CANDIDATES`), timeouts, and space headroom (~2× the image size).
The target filesystem is **discovered from each device's own q-filesystem
data** (`flash:` on Catalyst switches, `bootflash:` on C8000V) — if a platform
names its writable filesystem something else entirely, add it to
`TARGET_FS_CANDIDATES`; if the free-space read fails, check
`DATA_Q_FILESYSTEM` for your release.

## Reuse & licensing analysis

This project is **Apache-2.0** (see [`LICENSE`](LICENSE)). The up-front analysis
looked hard for something to reuse before writing code:

- **No permissive OSS library ships a turnkey "upgrade IOS-XE" function**, and
  **none of the Nautobot OSS apps** (Device Lifecycle Mgmt, Golden Config,
  device-onboarding, nornir-nautobot) ship a software-install/upgrade job. So the
  orchestration here is new — but it deliberately **reuses Nautobot core** for
  all data (software versions, images, hashes, credentials) and uses only
  **`requests`** for transport.
- **Cisco pyATS/Genie "Clean"** (Apache-2.0) is the best open reference for
  correct install-mode sequencing; it was used as a **design reference only**, not
  a runtime dependency (it's heavy and unnecessary for RESTCONF).
- ⚠️ **Avoided on licensing grounds:** the `cisco.ios` Ansible collection and
  community IOS-XE upgrade Ansible roles are **GPLv3** (copyleft) — their code is
  **not** copied here, only their behavior studied. Network to Code's commercial
  **"OS Upgrades"** Nautobot app is **closed-source** — reference only.

Everything actually depended on (`requests`, Nautobot core) is permissive
(Apache-2.0 / MIT) and compatible with this repo's license.

## Known limitations / not yet done

- **Hardware validation covers 17.12, 17.15, 17.18, and 26.1** on single
  switches and a 2-member stack, from Nautobot 3.1 and 2.4 — other platforms
  (9200, 9400–9600, C8000V) are admitted on model evidence (see
  [Versions & support](#versions--support)); do one supervised run
  per newly-encountered train or platform. On releases whose devices don't populate
  the operation ledger or `sys-activity` at runtime, the job degrades to
  version-state inference and a settle timer — clearly labeled in the logs.
- **The activate deliberately does NOT send `auto-abort-timer-val`** (the leaf
  triggered a fatal ISSU compatibility check on real hardware); the platform's
  default rollback timer applies instead and is confirmed after reload
  (observed arming at 7200 s on 17.15.x).
- Free-space and file-size reads use **release-dependent** q-filesystem paths
  (exact/stack-suffix partition match) — tunable via `constants.py` if a
  platform names its flash differently.
- Stack/SVL handling checks that **all members** report install mode, have the
  free space, and rejoin after reload; per-member deep health checks are
  minimal.
- Some IOS-XE releases show harmless SELinux AVC bursts around filesystem
  listings — see [SELinux AVC log events](#selinux-avc-log-events-cause-and-workaround)
  for the cause, when the job triggers them, and the optional quieting.

## SELinux AVC log events (cause and workaround)

**What they are.** On affected IOS-XE releases, the platform's SELinux policy denies `smand`
(the shell/storage manager) read access to a handful of on-flash paths
(`biosupgrade`, `yang-infra`, and similar) that it touches whenever it builds
a **filesystem listing**. Each listing therefore sprays a burst of
`%SELINUX-1-VIOLATION` AVC-denial lines (~100 observed per listing on a real
9300). This is a **Cisco policy defect and cosmetic**: the denials do not
fail the operation — the listing still returns, transfers and installs are
unaffected. Anything that requests a listing triggers it: this job's
file reads, human `show` commands (`dir`, `show platform software ...`),
and the install engine's own add/activate/clean activity.

**Why the job triggers them.** The job asks the device about files the
simple way: the standard full q-filesystem listing, used for the copy
pre-check, per-poll transfer progress, and the byte-exact verify. On an
affected release each of those listings logs one burst on unquieted
terminals. Earlier versions of this job carried a tiered read design
(keyed per-file reads, an image-catalog side channel, ledger mount-root
inference) purely to dodge this cosmetic defect; it was removed
deliberately (2026-07-10) — reliability and simplicity outrank quiet
reads for cosmetic, harmless messages the Quiet option handles
directly. Two aspects of the read design
survive on their own merits: the partition-stats reads (discovery and
the free-space gate) stay `fields`-scoped — a payload/parse-size choice;
on affected releases the device still walks server-side, so those two
reads burst like any other listing — and every fallback (a release
rejecting `fields`) still logs a breadcrumb attributed to its device.

**Job-managed quieting (opt-in).** The messages are harmless — a result
of how the job (and any `show` command) watches files on the filesystem —
so most operators can simply ignore them. But if your upgrade process
involves watching the physical console or terminal-monitor over SSH, you
may want to enable the *Quiet SELinux log noise on terminals* checkbox to
quiet them. It makes the job apply the workaround itself, scoped to where
the noise actually bothers people: it inserts the `NBAVC` discriminator
into the **running config** as early in the run as possible (so even the
gates read is filtered) and attaches it to the **physical console and
terminal-monitor (SSH) sessions only**. The `show logging` buffer and
syslog hosts deliberately stay unfiltered — they are the record (genuine
SELinux events share this facility and remain fully visible there); the
terminals are the noise. The filter is applied on every release (the
messages are not tied to one train). The job never replaces an existing
operator discriminator, logging mode, or an operator-owned `NBAVC` entry
with different content; `no logging console/monitor` and filtered/XML
modes are skipped rather than flipped; and any refused write warns instead
of failing the run. The change is unsaved — the activation reload erases it
— unless combined with *Save running-config before reload* on a Full run,
which makes it persistent. Confirm with `show run | include NBAVC` —
three lines:

```
logging discriminator NBAVC facility drops SELINUX
logging console discriminator NBAVC
logging monitor discriminator NBAVC
```

**Manual workaround** (same effect, applied by hand — suppresses the
cosmetic denials from the console/buffer without touching the underlying
policy):

```
logging discriminator NOSEL msg-body drops SELINUX
logging console discriminator NOSEL
logging buffered discriminator NOSEL
```

Remove the discriminator after the upgrade window if you prefer to keep
SELinux visibility day-to-day.

## Deferred (by agreement — not built yet)

These were intentionally left out to keep the first cut small; revisit as
separate, agreed features:

- A companion job to **enable RESTCONF** on devices that lack it (needs a
  non-RESTCONF channel to bootstrap).
- **Native ISSU mode for 9400/9500/9600 HA pairs**: `issu: true` on the
  activate (the RPC leaf already exists) plus ISSU-aware confirmation —
  today's logic requires observing the device go DOWN, which an ISSU
  deliberately avoids. Gated on a lab SVL pair for the research spike; see
  the proposed interim workflow above.
- **Catalyst 9800 wireless-aware mode**: AP image predownload between add and
  activate (`Cisco-IOS-XE-wireless-access-point-cmd-rpc:set-rad-predownload-all`
  is available at our floor), AP-fleet completion polling, and SSO awareness —
  until then the job warns and leaves 9800s to deliberate full-outage use.
- **Device Lifecycle Management** integration for **validated/approved-software
  gating** and CVE/EoL/contract context.
- User-based **authorization/gating** of who may run upgrades.
- Deeper stack/redundancy and post-upgrade interface/protocol health checks.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).

## Disclaimer

This software is provided **"AS IS"**, without warranties or conditions of any
kind, under the terms of the [Apache License 2.0](LICENSE) — including its
**Disclaimer of Warranty (§7)** and **Limitation of Liability (§8)**:

- **No warranty.** There is no warranty of any kind, express or implied —
  including, without limitation, any warranties of merchantability, fitness
  for a particular purpose, title, or non-infringement. You are solely
  responsible for determining the appropriateness of using this software and
  assume all risks of doing so.
- **No liability.** In no event shall the authors, contributors, or copyright
  holders be liable for any damages of any character arising from the use or
  inability to use this software — including, without limitation, network
  outages, device or hardware failure, data loss, loss of profits, or any
  other commercial damage — even if advised of the possibility of such
  damages.

Be aware of what this tool does: it **copies software to, and reloads, live
network equipment**. If you choose to run it in your own environment, you do so
entirely **at your own risk** — validate in a lab first (see
[Current status](#current-status-lab-proven)), keep Dry-run on until proven, and
maintain your own change-control and rollback procedures. Use of this software
constitutes acceptance of the license terms above.
