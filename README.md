# nautobot-upgrades

A native **Nautobot Job** that reliably and cautiously upgrades **Cisco IOS-XE**
devices — **Catalyst 9300** primarily — driven entirely over **RESTCONF**.

> ### Status: work in progress — core flow thoroughly lab-proven, active development ongoing
>
> The core upgrade/downgrade flow is **thoroughly tested on real Catalyst
> 9300 hardware**: repeated upgrades **and downgrades**, entirely over
> RESTCONF, across **17.12 → 17.15 ↔ 17.18 ↔ 26.1** — single switches, a
> **2-member stack**, **lettered rebuilds** (17.15.4 ↔ 17.15.4d), serial
> **batches** (including correct already-on-target short-circuits and a batch
> downgrade), remove-inactive cleanup, and interrupted-run recovery — all
> driven by the operation-ledger tracking and engine-idle gating the job
> decides by. That said, this project is under **active development**: new
> capabilities land frequently (parallel batches and pre-staging are the most
> recent), each marked with its validation state in
> [Status & testing](#status--testing). Expect change between releases, read
> the Job Result logs, and **always run with Dry-run first**.

---

## What it does

From the Nautobot **Jobs** page you scope a set of target devices — with optional
filters for **location, role, status, platform, device type, current version, and
tags** — pick a target software version, and the job performs an **install-mode**
upgrade the way a conservative engineer would — as a series of PASS/FAIL gates,
stopping on the first failure for a device:

1. **Connect** — resolve the device's primary IP and credentials (from core
   Secrets), confirm RESTCONF is reachable.
2. **Pre-flight gates** — read the running version; skip if already on target;
   confirm the device is **≥ 17.9.1** and in **install mode**; resolve the image
   from Nautobot and confirm device-type compatibility; **confirm enough free
   space** before copying anything.
3. **Transfer + integrity** — the device pulls the image via the **classic
   copy RPC** (run in a worker thread) while the job polls the growing on-device
   file, logging **progress (MB / % / elapsed)**, and requiring an **exact size
   match** when the transfer finishes — backed by `install add`'s mandatory
   image signature validation (which aborts on a corrupt/untrusted image). If
   the exact file is already on flash, the copy is skipped (idempotent re-runs).
   The fancier async `xcopy` RPC was deliberately abandoned after a real
   17.15.05 silently broke it while the classic path kept working.
4. **Install** — every engine write follows one pattern: **gate → submit →
   track**. The job waits for the engine to report **idle** (`sys-activity`),
   submits with a per-operation uuid, then tracks that uuid in the device's own
   **operation ledger** (`install-oper`/`install-oper-hist`) to true
   op-completion — never trusting the RPC's 2xx, which the engine returns even
   when it refuses. `install add` → engine-idle gate → `install activate`
   (**explicitly non-ISSU**, by the device's **full internal version
   identifier**; silently-dropped requests are detected via the ledger and
   re-sent) → reload, with the **auto-rollback timer** checked after boot.
   Ledger-recorded failures abort quoting the engine's own failing phase; on
   releases that don't populate these signals, the job degrades to
   version-state inference and a settle timer — labeled as fallbacks in the
   logs.
5. **Verify, then commit** — reconnect, confirm the device actually booted the
   target version, and **only then** `install commit`. If it didn't come back or
   booted the wrong version, the job does **not** commit and the device
   auto-rolls-back to the previous image.
6. **Sync + (optional) cleanup** — update `Device.software_version` in Nautobot;
   optionally `install remove inactive` to reclaim space (off by default).

Feedback is mandatory and built in: every gate logs to the Job Result with the
device attached, and a **Debug** toggle logs every RESTCONF request/response.
Batches run **in parallel** — see [Parallel batches](#parallel-batches) for
mechanics, sizing guidance, and the time-budget behavior.
Per-device failures don't stop the batch (the remaining devices still run),
but **any device failure marks the whole Job Result FAILED** at the end — a
green job means every selected device succeeded. **Durations are logged for
change-window planning**: each device's result carries its total wall-clock
time, and the reload reports the outage window (unreachable-for and
reload-to-confirmed times), alongside the existing copy and install phase
timings.

See **[docs/upgrade-flow.md](docs/upgrade-flow.md)** for a flowchart of the
per-device decision logic (editable [`upgrade-flow.drawio`](docs/upgrade-flow.drawio)).

## Supported versions

| Component | Supported | Notes |
| --- | --- | --- |
| **Nautobot** | **2.4 LTM** and **3.1+** | End-to-end upgrades verified from **3.1 and multiple independent 2.4 environments** (most testing volume on 3.1). **3.0 is untested and will stay that way** — it no longer receives maintenance now that 3.1 (the 3.x LTM designation) has shipped. Earlier 2.x (≥ 2.2) *may* work but is not tested or supported. |
| **Deployment** | [nautobot-composer](#sister-project-nautobot-composer) | The sister Docker-Compose installer this Job is built to run on; it currently ships Nautobot 2.4 and 3.x. |
| **Device OS** | Cisco IOS-XE **≥ 17.9.1** (incl. 26.x) | Hardware-validated on **17.15.x**; every YANG model the job touches verified against Cisco's published models from 17.9.1 through 26.1.1. See the [support posture](#support-posture) for the per-train breakdown. Model presence ≠ runtime behavior — run one supervised upgrade per new train before fleet use. Rebuild letters (e.g. 17.15.4**d**) are **distinct versions** — base → rebuild upgrades (and rebuild rollbacks) are supported. |
| **Platform** | Catalyst **9300 family** + **C8000V** | 9300 hardware-tested; **9300L/LM/X** run the identical cat9k image, install flow, and YANG bundle (validation run pending). **Catalyst 8000V** (autonomous mode): all required models verified in Cisco's c8000v capability files; its `bootflash:` filesystem is **discovered from the device** (hardware run pending). **Catalyst 9200** and **9400/9500/9600**: model sets verified identical — validation runs pending. **Catalyst 9800 WLC**: mechanically compatible but **operationally out of scope** — the job upgrades the controller only, with no AP predownload; a full-scope run is warned in-job (extended wireless outage) and a wireless-aware mode is planned. Nexus/NX-OS is a different OS and API — not supported. **3650/3850 cannot be supported** (terminal 16.12 train lacks the install API; both at/near end of support — Cisco's replacement is the 9300L, which this job supports). |

### Support posture

The posture is deliberate, in priority order:

1. **17.15, 17.18, and 26.1 first** — current mainline code, all
   hardware-tested; the platforms this job is built and validated against.
2. **17.12** — aging but still supportable mainline; **hardware-validated in
   both directions** (a 2-member stack was lifted 17.12.4 → 17.15.5, and a
   downgrade to 17.12.6 completed successfully). Prefer current mainline as
   an upgrade target; the 17.12 rollback path is proven.
3. **17.9 – 17.11** — **not tested, but might work**: model-complete on paper
   (17.9 is the floor), best suited as an escape source for parked fleets.
4. **Older than 17.9** — **not supported.** The job cannot execute as
   written there — key API components are missing — so it refuses these
   releases.

| IOS-XE train | Status | Basis |
| --- | --- | --- |
| **17.12 / 17.15 / 17.18 / 26.1** | ✅ **Tested and working on real equipment** | Repeated full upgrades **and** downgrades across all four trains on Catalyst 9300s — single switches and a **2-member stack** (17.12.4 → 17.15.5 → 17.15.4), the **lettered rebuild cycle** (17.15.4 ↔ 17.15.4d), cross-era moves in both directions (17.15.5 ↔ 17.18.3, 17.15.5 → 26.1.1, 26.1.1 → 17.18.3 and back down to 17.15.x), and serial **batches** including a batch downgrade. Ledger-tracked add/activate/commit, engine-idle gating, member-rejoin gate, byte-exact copy verification, remove-inactive, and interrupted-run recovery all exercised live. |
| **17.9 / 17.10 / 17.11** | ⚠️ **Not tested — might work** | Model-complete on paper (17.9 is the support floor; every YANG model the job touches verified against Cisco's published 17.9.1–17.11.1 models). Best suited as an *escape source*: 17.9 exited Cisco software maintenance in Aug 2025 — upgrade FROM it rather than to it. Run one supervised upgrade before relying on it. |
| **< 17.9** | 🚫 **Not supported** | The job refuses these releases because it **cannot execute as written: key API components are missing** below the floor (the RESTCONF install models and reliable file-size reporting the job is built on). |

**Nautobot**: installed, synced, and **job execution verified on 3.1 and
multiple independent 2.4 environments** (incl. a full 26.1.1 → 17.18.3 device
install from a stock 2.4.36 outside nautobot-composer). Most testing volume
remains on 3.1.

There is no separate Python dependency matrix: the Job imports only `requests`
plus Nautobot core, so whatever ships with the supported Nautobot release suffices.

## Status & testing

Hardware validation now spans **trains 17.12 → 17.15 ↔ 17.18, a 2-member
stack, and lettered rebuilds** — all from Nautobot 3.1.

**Verified on real hardware (Catalyst 9300, single switch + 2-member stack)**

- ✅ **Full upgrade AND downgrade end-to-end, repeatedly**: reachability/auth,
  all pre-flight gates, threaded classic copy with live progress and
  **byte-exact size verification**, ledger-tracked `install add`, engine-idle
  gate, full-internal-version activate (with drop detection + re-send), reload,
  stable-boot confirm, ledger-confirmed commit, Nautobot sync.
- ✅ **A 2-member stack**, up and down a train boundary: 17.12.4 → 17.15.5 →
  17.15.4 — per-member free-space gating, package distribution, and the
  **all-members-rejoined gate** all ran live.
- ✅ **Cross-train moves in both directions**: 17.12 → 17.15, 17.15 ↔ 17.18
  (17.15.5 → 17.18.3 → 17.15.5 on a single switch).
- ✅ **26.1 in both directions**: single switch 17.15.5 → 26.1.1; batch
  downgrade 26.1.1 → 17.15.4d (a lettered rebuild as the batch target).
- ✅ **Batch mode (serial)**: a mixed batch (single switch + 2-member stack)
  targeting 26.1.1 — the already-on-target device short-circuited correctly
  while the batch proceeded — and a full batch downgrade.
- ✅ **Lettered rebuilds as distinct versions**: 17.15.4 → **17.15.4d** →
  17.15.4 — upgrade and rollback.
- ✅ **Operation-ledger tracking live on-device**: op records keyed by the
  job's own uuids, per-phase engine statuses driving the gates.
- ✅ **Interrupted-run recovery** (commit-to-be-safe): a re-run against an
  already-on-target, uncommitted device commits it and re-syncs Nautobot.
- ✅ **Idempotent re-runs**: copy skipped when the exact file is on flash;
  add skipped when already staged.
- ✅ **Rollback timer** confirmed arming on real activations.
- ✅ **Remove inactive**: ledger-confirmed by the job and CLI-verified on the
  switch (nothing left to delete afterward).
- ✅ **Register Image checksum verification**: worker-computed MD5 over the
  internal repo route matched Cisco's published value (~3 s for a full image —
  the transfer never leaves the Docker host).
- ✅ Installs / syncs as a Git Repository on **Nautobot 2.4 and 3.1**; both Jobs
  register. **Register IOS-XE Image**: upload → validate → record.
- ✅ **Job execution from Nautobot 2.4.36**: a full 26.1.1 → 17.18.3 device
  install ran end-to-end from a stock 2.4.36 outside nautobot-composer.
- ✅ **Pre-staging exercised**: a version staged via Run scope was correctly
  held on the device, and a follow-up run targeting a *different* version was
  correctly refused by the install engine (the failure condition behaves).
- ✅ A long list of real-device truths encoded and regression-tested: boot-mode
  leaf naming, version-state semantics, silent RPC drops during the post-add
  compatibility probe, junk version identifiers, KB-vs-byte size units.

**Not yet tested — do not assume these work**

- 🔶 **Parallel batch execution** — implemented and running in the lab; the
  first parallel run surfaced worker-thread log loss (fixed — Celery task +
  request context propagation). Recent feature: watch the first runs. Serial
  batches (Parallelism = 1) are fully validated.
- 🔶 **Pre-staging (Run scope)** — recent feature: staging itself has been
  exercised on hardware, but the full timed cycle (stage midday → window run
  collapsing to ~reload time) hasn't been measured yet. It is a strict subset
  of the validated full flow that stops before activate.
- ❌ **17.9 / 17.10 / 17.11** — not tested; might work (see the support
  matrix). **Nautobot 3.0** is untested by choice: unmaintained since 3.1
  shipped.
- ❌ **9300L/LM/X validation run** (identical image/flow — expected boring)
  and the **C8000V hardware run** (filesystem discovery + models verified on
  paper); other 9300 models beyond those in the lab; stacks larger than
  2 members; 17.18/26.1 on a stack.
- ❌ **Failure paths on hardware**: auto-rollback expiry (activate without
  commit), a genuinely corrupt image, a member failing to rejoin.


**Suggested test order (lab only)**

1. **Install + register.** Sync the repo on a **Nautobot 3.1** nautobot-composer
   instance (the platform all hardware testing ran from) and enable both Jobs;
   upload a `.bin` to the firmware server and run **Register IOS-XE Image** with
   Dry-run, then for real; confirm the resulting `SoftwareImageFile` /
   `SoftwareVersion` look correct.
2. **Upgrade Dry-run.** Against one lab Catalyst 9300 (≥ 17.9.1, RESTCONF enabled,
   a Secrets Group assigned): run **Cisco IOS-XE Upgrade** with Dry-run on and
   confirm the reachability/auth, version-floor, install-mode, image-resolution,
   and free-space gates all read correctly (the target filesystem is
   discovered from the device automatically; `TARGET_FS_CANDIDATES` in
   [`jobs/constants.py`](jobs/constants.py) covers platforms that name it
   differently).
3. **Single real upgrade.** One non-production device — watch the Job Result log
   through copy → add → activate → reload → confirm → commit, and verify the
   auto-rollback timer actually arms.
4. **Broaden.** One supervised run per additional IOS-XE train (26.1 is the
   open one), then multi-device batches, before any wider use.

Until at least steps 1–3 pass in a lab, treat every run as experimental and keep
Dry-run on.

## Why these design choices

The design follows a deep up-front analysis to avoid reinvention and respect the
project's constraints. The key findings that shaped it:

- **RESTCONF can drive the entire upgrade — on modern IOS-XE.** The install
  workflow is exposed via the `Cisco-IOS-XE-install-rpc` YANG model
  (`install` / `activate` / `install-commit` / `remove`), and the image transfer
  via the classic `Cisco-IOS-XE-rpc:copy` — run in a worker thread so the job
  can poll copy **progress** from the on-device file size. The
  support floor is **17.9.1** — the lowest model-complete release; the job
  refuses anything older (see the support posture above). (The async `xcopy` transfer was tried and
  abandoned: 17.15.05 silently broke it in the field.)
- **Image-file integrity is deliberately covered without the on-device
  `verify` RPC.** Three layers: the Register job can download and
  **hash-verify** the image server-side at registration (md5…sha512); every
  copy ends with a **byte-exact size match** against Nautobot's recorded size
  (tolerance 0); and `install add` performs Cisco's **mandatory signature
  validation** of the signed image before anything can activate — strictly
  stronger than an MD5 self-check, since it catches corruption *and*
  tampering. The `Cisco-IOS-XE-verify-rpc:verify` RPC does exist (17.12+,
  md5/sha512) but returns only a correlation UUID, with results delivered via
  **event notifications** and no pollable operational state — the same
  async-invisible shape as the abandoned `xcopy` — so it was considered and
  deliberately not adopted. If Cisco ever exposes a pollable verify result,
  it becomes a small, clean addition.
- **Software version/image data is now Nautobot _core_, not a plugin.**
  `dcim.SoftwareVersion` and `dcim.SoftwareImageFile` moved into core in
  **Nautobot 2.2** (image file name, checksum + hashing algorithm, file size,
  download URL, default-image flag, device-type compatibility), and
  `Device.software_version` tracks assignment. We read all of this from core and
  add **no data models of our own**.
- **Delivery via Git Repository, not a packaged app.** This is the lightweight,
  idiomatic way to ship jobs from public GitHub. Its one constraint —
  git-delivered jobs **cannot install their own pip dependencies** — is a perfect
  fit here because the job depends only on **`requests`** (RESTCONF over
  HTTPS+JSON) and Nautobot core, both always present. **Zero new dependencies.**

## Requirements

**Nautobot side**

- Nautobot **2.4 LTS** or **3.x** (see [Supported versions](#supported-versions)) —
  typically deployed via [nautobot-composer](#sister-project-nautobot-composer),
  the sister installer this Job targets.
- The repository added as a **Git Repository** with the **`jobs`** provided
  contents type (see below).
- Each target device must have:
  - a **primary IPv4** address reachable from the Nautobot worker;
  - an assigned **Secrets Group** (or pass one as a job override) exposing a
    **username** and **password** under the **RESTCONF** access type (see
    [Authentication](#authentication));
  - a **device type** mapped to the target version's **Software Image File**
    (core's compatibility map), or a default image on the version.
- A **Software Version** record for the target, with a **Software Image File**
  that has at least a **download URL** and **image file name** (set the **file
  size** too, to enable the post-copy size-integrity gate).

**Device side**

- Cisco IOS-XE **≥ 17.9.1**, Catalyst 9300, booted in **install mode**
  (`flash:packages.conf`).
- **RESTCONF enabled** (`restconf` + `ip http secure-server`). Enabling RESTCONF
  on devices that lack it is intentionally **out of scope** for now.
- A RESTCONF login account at **privilege 15** (or with exec authorization for
  `install`/`copy`); a lower-privilege account authenticates but cannot run the
  upgrade.
- The image **download URL** must be reachable by the device over a transport it
  supports (https/http/scp/ftp/tftp); embed credentials in the URL if required.

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

## Sister project: nautobot-composer

This Job is designed to run on **nautobot-composer** — a Docker-Compose-based
installer for Nautobot and several related tools, by the same author, which
currently supports **Nautobot 2.4 and 3.x**. It is also where the **firmware
server** lives (its opt-in `firmware` profile — see [Image storage](#image-storage)).

You can run this Job on any Nautobot 2.4/3.x instance, but nautobot-composer is
the reference deployment: it provides a matching Nautobot version, the firmware
host devices pull images from, and a worker environment that already has the
Job's only runtime dependency (`requests`, plus Nautobot core). Tested to date:
installs on 2.4 and 3.1 nautobot-composer deployments; the end-to-end device
upgrade ran from 3.1.

## Installing into Nautobot

1. Ensure `requests` and Nautobot core are present (they always are). **No extra
   packages are needed.**
2. In Nautobot: **Extensibility → Git Repositories → Add**, set the repository
   URL to this public repo, choose a branch, and select **Provides: Jobs**, then
   **Sync**.
3. **Jobs → Jobs**: under the **IOS-XE Upgrades** group, edit and **Enable**
   **“Cisco IOS-XE Upgrade (RESTCONF)”**, **“Register IOS-XE Image”**, and
   **“Cancel IOS-XE Upgrade Run”**.
4. After changing job code, re-sync the repo and (for non-container installs)
   restart the Celery worker.

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
don't have; our upgrade is reload-based by design), and 17.15.x emits SELinux
`%SELINUX-1-VIOLATION` AVC-denial spam for `smand`/`yang-infra` that is unrelated
to the upgrade. The repeated `%DMI-5-AUTH_PASSED` entries are this job's own
RESTCONF polling.

### Parallel batches

Batch runs upgrade up to **Parallelism** devices concurrently (default **4**,
range 1–16; `1` = strictly one at a time). An upgrade is ~90 % waiting — copy,
install, reload — so parallelism collapses batch wall-clock dramatically: a
12-device batch at parallelism 4 is ~3 waves ≈ 90 minutes instead of ~6 hours
serial. Each device's result line carries its own `[total: …]` for the
change-window arithmetic.

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

Staging causes **no outage**, so it is safe at high **Parallelism** during
business hours, and pairs naturally with Nautobot's native job scheduling
("stage the fleet overnight"). Structural guarantee: stage scopes return
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
| Clean device first | no | ⚠️ **Default off.** Before upgrading, remove ALL software the device is not running — inactive packages, leftover files, **and any version another engineer staged** (deliberately overrides the staged-conflict stop) plus the soak-period rollback image. For engineers who know the state of the network. Failures abort; dry-run reports what would be removed. Independent of *Remove inactive (after commit)*. |
| Run scope | no | Order of operations, safest first: **Step 1 - Copy image** (**default** — a forgotten dropdown can never reload a device), **Steps 1 & 2 - Copy image and prep** (`install add`, no reload), **Full - Copy, Activate, Reload** (the only choice that reloads; a real upgrade requires selecting it deliberately). See [Pre-staging](#pre-staging-stage-now-activate-in-the-window). |
| Secrets group override | no | Force one Secrets Group for the whole run; by default each device uses its own assigned group. |
| Remove inactive | no | After commit, reclaim space (default **off** — keeps the rollback image for a soak period). |
| Parallelism | no | Devices upgraded concurrently (default **4**, max 16; 1 = serial). Size to the firmware server's capacity for simultaneous image pulls. |
| Debug | no | Verbose RESTCONF request/response logging. |
| Dry-run | — | Read-only pre-flight only (default **on**). |

### RESTCONF operations used

| Step | RESTCONF call |
| --- | --- |
| Read version | `GET .../Cisco-IOS-XE-device-hardware-oper:device-hardware-data/device-system-data` |
| Install state / mode | `GET .../Cisco-IOS-XE-install-oper:install-oper-data` |
| Free space / file size | `GET .../Cisco-IOS-XE-platform-software-oper:cisco-platform-software/q-filesystem` |
| Copy image (+ progress) | `POST .../operations/Cisco-IOS-XE-rpc:copy` (worker thread), size-polled via q-filesystem |
| Add / activate / commit / remove | `POST .../operations/Cisco-IOS-XE-install-rpc:{install,activate,install-commit,remove}` |

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
  (9200, 9400–9600, C8000V) are admitted on model evidence (see the
  [support matrix](#support-matrix) and platform row); do one supervised run
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
  minimal. **17.15.x devices emit an SELinux AVC log flood** during
  q-filesystem polling (Cisco policy defect, cosmetic — suppressible with a
  `logging discriminator`; see the run notes above).

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
entirely **at your own risk** — validate in a lab first, follow the
[suggested test order](#status--testing), keep Dry-run on until proven, and
maintain your own change-control and rollback procedures. Use of this software
constitutes acceptance of the license terms above.
