# nautobot-upgrades

A native **Nautobot Job** that reliably and cautiously upgrades **Cisco IOS-XE**
devices — **Catalyst 9300** primarily — driven entirely over **RESTCONF**.

> ### ⚠️ Status: new — installs on Nautobot 2.4; functionality not yet tested
>
> The package **installs and syncs** as a Git Repository on **Nautobot 2.4**
> (deployed via [nautobot-composer](#sister-project-nautobot-composer)). Beyond
> that, **nothing has been exercised**: no Job has been run end-to-end, no real
> device has been touched, and other Nautobot versions (3.x) have not been tried.
> The RESTCONF payloads and operational paths are **research-derived** from
> Cisco's published YANG models, not confirmed on a device. Treat this as a
> **lab-only first draft** and **always run with Dry-run first**. Expect to adjust
> release-specific details in [`jobs/constants.py`](jobs/constants.py). See
> [Status & testing](#status--testing) for what is and isn't verified.

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
   confirm the device is **≥ 17.3.1** and in **install mode**; resolve the image
   from Nautobot and confirm device-type compatibility; **confirm enough free
   space** before copying anything.
3. **Transfer + integrity** — device pulls the image (`copy` RPC), then the job
   **verifies the image arrived intact** by confirming the on-device file size
   matches the expected size, backed by `install add`'s mandatory image
   signature validation (which aborts on a corrupt/untrusted image). The
   on-device hash RPC is intentionally not used as a gate — it is asynchronous
   and returns no synchronous pass/fail.
4. **Install** — `install add` → `install activate` (which arms the device's
   **auto-rollback timer**) → reload.
5. **Verify, then commit** — reconnect, confirm the device actually booted the
   target version, and **only then** `install commit`. If it didn't come back or
   booted the wrong version, the job does **not** commit and the device
   auto-rolls-back to the previous image.
6. **Sync + (optional) cleanup** — update `Device.software_version` in Nautobot;
   optionally `install remove inactive` to reclaim space (off by default).

Feedback is mandatory and built in: every gate logs to the Job Result with the
device attached, and a **Debug** toggle logs every RESTCONF request/response.

See **[docs/upgrade-flow.md](docs/upgrade-flow.md)** for a flowchart of the
per-device decision logic (editable [`upgrade-flow.drawio`](docs/upgrade-flow.drawio)).

## Supported versions

| Component | Supported | Notes |
| --- | --- | --- |
| **Nautobot** | **2.4 LTS** and **3.x** | The intended targets. Earlier 2.x (≥ 2.2, where the core `SoftwareVersion` / `SoftwareImageFile` models exist) *may* work but is **not tested or supported**. |
| **Deployment** | [nautobot-composer](#sister-project-nautobot-composer) | The sister Docker-Compose installer this Job is built to run on; it currently ships Nautobot 2.4 and 3.x. |
| **Device OS** | Cisco IOS-XE **≥ 17.3.1** | Where the RESTCONF install models exist; the Job refuses lower releases. **16.12.x is not supported.** |
| **Platform** | Catalyst **9300** (install mode) | Primary target, booted from `flash:packages.conf`. |

There is no separate Python dependency matrix: the Job imports only `requests`
plus Nautobot core, so whatever ships with the supported Nautobot release suffices.

## Status & testing

This project is **new and largely unverified** — be conservative with it.

**Verified so far**

- ✅ The repository **installs / syncs cleanly as a Nautobot Git Repository on
  Nautobot 2.4** (deployed via nautobot-composer): both Jobs register and appear
  on the Jobs page.

**Not yet tested — do not assume these work**

- ❌ **End-to-end Job execution.** Neither *Cisco IOS-XE Upgrade* nor *Register
  IOS-XE Image* has been run, even in Dry-run.
- ❌ **Any interaction with a real device** (RESTCONF reachability, copy, install,
  reload, commit) — every RESTCONF leaf path / RPC payload is research-derived.
- ❌ **Nautobot 3.x** — only 2.4 has been installed against.
- ❌ The **firmware-server integration** (upload → register → device pull).
- ❌ The **auto-rollback timer** behavior and the `install-oper` state parsing on
  real hardware (see [Known limitations](#known-limitations--not-yet-done)).

**Suggested test order (lab only)**

1. **Install + register.** Sync the repo on a 2.4 nautobot-composer instance and
   enable both Jobs; upload a `.bin` to the firmware server and run **Register
   IOS-XE Image** with Dry-run, then for real; confirm the resulting
   `SoftwareImageFile` / `SoftwareVersion` look correct.
2. **Upgrade Dry-run.** Against one lab Catalyst 9300 (≥ 17.3.1, RESTCONF enabled,
   a Secrets Group assigned): run **Cisco IOS-XE Upgrade** with Dry-run on and
   confirm the reachability/auth, version-floor, install-mode, image-resolution,
   and free-space gates all read correctly. Fix any release-specific leaf paths in
   [`jobs/constants.py`](jobs/constants.py).
3. **Single real upgrade.** One non-production device — watch the Job Result log
   through copy → add → activate → reload → confirm → commit, and verify the
   auto-rollback timer actually arms.
4. **Broaden.** Repeat on **Nautobot 3.x** and on a **stack** before any wider use.

Until at least steps 1–3 pass in a lab, treat every run as experimental and keep
Dry-run on.

## Why these design choices

The design follows a deep up-front analysis to avoid reinvention and respect the
project's constraints. The key findings that shaped it:

- **RESTCONF can drive the entire upgrade — but only on IOS-XE ≥ 17.2.1.** The
  install workflow is exposed via the `Cisco-IOS-XE-install-rpc` YANG model
  (`install` / `activate` / `install-commit` / `remove`), invokable over
  RESTCONF. **That model does not exist on 16.12.x** (verified against Cisco's
  published YANG models — it first appears in 17.2.1, with `install-oper` in
  17.3.1). On a box currently running 16.12.x the install step has **no RESTCONF
  path** at all. We therefore **require ≥ 17.3.1** and the job refuses lower
  releases with guidance, rather than dragging in a second protocol.
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

- Cisco IOS-XE **≥ 17.3.1**, Catalyst 9300, booted in **install mode**
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

Configure on the Nautobot worker: `FIRMWARE_BASE_URL` (device-facing base, e.g.
`https://<host>:9443/images/`) and `FIRMWARE_INTERNAL_URL` (worker validation,
default `http://firmware-download/images/`). Both are overridable per run.

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
Job's only runtime dependency (`requests`, plus Nautobot core). The one
installation tested to date is a 2.4 nautobot-composer deployment.

## Installing into Nautobot

1. Ensure `requests` and Nautobot core are present (they always are). **No extra
   packages are needed.**
2. In Nautobot: **Extensibility → Git Repositories → Add**, set the repository
   URL to this public repo, choose a branch, and select **Provides: Jobs**, then
   **Sync**.
3. **Jobs → Jobs**: under the **IOS-XE Upgrades** group, edit and **Enable**
   both **“Cisco IOS-XE Upgrade (RESTCONF)”** and **“Register IOS-XE Image”**.
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

### Job inputs

| Input | Required | Purpose |
| --- | --- | --- |
| Location / Role / Status / Platform / Device type / Current version / Tags | no | Optional filters that narrow the **Devices** picker for field operations. |
| Devices | yes | Target devices to upgrade (narrowed by the filters above). |
| Target version | yes | Core `SoftwareVersion` to upgrade to. |
| Secrets group override | no | Force one Secrets Group for the whole run; by default each device uses its own assigned group. |
| Assume install mode | no | Proceed when boot mode can't be confirmed over RESTCONF (default **off** = fail closed; confirmed BUNDLE always aborts). **Required for devices on 17.3.1–17.4.x** — the install-oper `boot-mode` leaf only exists from **IOS-XE 17.5.1**; verify install mode manually first. |
| Remove inactive | no | After commit, reclaim space (default **off** — keeps the rollback image for a soak period). |
| Debug | no | Verbose RESTCONF request/response logging. |
| Dry-run | — | Read-only pre-flight only (default **on**). |

### RESTCONF operations used

| Step | RESTCONF call |
| --- | --- |
| Read version | `GET .../Cisco-IOS-XE-device-hardware-oper:device-hardware-data/device-system-data` |
| Install state / mode | `GET .../Cisco-IOS-XE-install-oper:install-oper-data` |
| Free space / file size | `GET .../Cisco-IOS-XE-platform-software-oper:cisco-platform-software/q-filesystem` |
| Copy image | `POST .../operations/Cisco-IOS-XE-rpc:copy` |
| Add / activate / commit / remove | `POST .../operations/Cisco-IOS-XE-install-rpc:{install,activate,install-commit,remove}` |

## Configuration

Release- and site-specific knobs live in [`jobs/constants.py`](jobs/constants.py):
the version floor, target filesystem (`flash:`) and its **partition-name match**
(`TARGET_FS_NAMES`), timeouts, and space headroom (~2× the image size). The
filesystem operational data path and partition naming can drift between IOS-XE
releases/platforms — if the free-space gate can't read anything, adjust
`DATA_Q_FILESYSTEM` / `TARGET_FS_NAMES` for your release.

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

- **Untested against hardware.** RESTCONF payload field names, operational leaf
  paths, and the install-state polling are research-derived and need lab
  validation. The following specifically require confirmation against a real
  device's `install-oper` data before production use:
  - **Auto-rollback timer:** the job arms it explicitly on `activate`
    (`auto-abort-timer-val`, research-derived leaf) and best-effort checks it is
    pending after reload, warning loudly if it can't confirm one. Whether a bare
    `activate` arms a timer, and the exact leaf name, are release-specific — the
    failure paths rely on this safety net, so verify it actually arms.
  - **Install-state classification:** `_classify_state` normalizes full enums and
    short codes (C/A/U/I), but the real `install-oper` state leaf names/values
    should be confirmed; commit/idempotency gates fail *safe* (commit-to-be-safe)
    if a state can't be classified.
- **16.12.x is not supported** (no RESTCONF install model on that train).
- Free-space and on-device file-size reads use **best-effort, release-dependent**
  paths (q-filesystem; exact/stack-suffix partition match) and may need tuning per
  release via `constants.py`.
- Stack/SVL handling checks that **all members** report install mode and that the
  system booted the target version; per-member deep health checks are minimal.

## Deferred (by agreement — not built yet)

These were intentionally left out to keep the first cut small; revisit as
separate, agreed features:

- A companion job to **enable RESTCONF** on devices that lack it (needs a
  non-RESTCONF channel to bootstrap).
- **Device Lifecycle Management** integration for **validated/approved-software
  gating** and CVE/EoL/contract context.
- **16.12.x support** via a NETCONF/CLI path for the install step.
- User-based **authorization/gating** of who may run upgrades.
- Deeper stack/redundancy and post-upgrade interface/protocol health checks.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
