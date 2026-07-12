# Image storage, upload & management

How the Cisco IOS-XE `.bin` images are stored, published, and tracked for the
upgrade job.

## Architecture: Nautobot is the index, the firmware server holds the bytes

The upgrade is **RESTCONF-only and device-initiated** — the switch pulls its own
image via the classic copy RPC (`Cisco-IOS-XE-rpc:copy`) from a URL we hand
it — while the job polls the growing file for progress. That dictates
the storage model:

```
            (metadata / index)                  (the bytes — companion stack)
   ┌────────────────────────────┐      ┌──────────────────────────────────────┐
   │ Nautobot core              │      │ "nautobot-composer" `firmware` profile │
   │  dcim.SoftwareVersion      │      │  • Filebrowser UI  :8088 (engineers)   │
   │  dcim.SoftwareImageFile    │      │  • nginx firmware-download             │
   │   • download_url ──────────┼─────▶│      :9080 http / :9443 https (devices)│
   │   • checksum + algorithm    │      │    shared volume, read-only, ACL       │
   │   • file size               │      └──────────────────────────────────────┘
   │   • mapped device types      │                 ▲              ▲
   └──────────────────────────────┘     device pull │              │ worker validates
                                          (copy RPC)  │              │ (internal HTTP)
                                       ┌─────────────┴──┐   ┌───────┴──────────┐
                                       │ Catalyst 9300  │   │ Nautobot worker  │
                                       └────────────────┘   └──────────────────┘
```

- **Nautobot stores only metadata** (`SoftwareImageFile`: file name, checksum +
  algorithm, size, `download_url`, default flag, device-type map). It does not
  store the binary and is not a device-facing file server.
- **The bytes live on any web server the devices can reach.** Any plain HTTP
  file server works — the device transfer is just a `GET`. The reference
  implementation below is the companion `nautobot-composer` stack's opt-in
  `firmware` profile (where all testing ran); it is one convenient option, not a
  requirement. Two services share one volume:
  - **Filebrowser** (`:8088`, authenticated) — engineers upload/manage images.
  - **nginx `firmware-download`** (`:9080` HTTP / `:9443` HTTPS, read-only) —
    serves the same files to devices, **unauthenticated but network/ACL
    restricted**. Directory listing off; GET/HEAD only; `Content-Type:
    application/octet-stream`; HEAD returns a correct `Content-Length`; byte-range
    supported.

## Download URL format

Device-facing (this is what gets stored in `download_url` and handed to the
device's `copy` RPC):

```
http://<host>:9080/images/<filename>      # DEFAULT — device TLS clients reject the self-signed cert
https://<host>:9443/images/<filename>     # opt-in per run ("Use HTTPS URL") once devices trust the cert
```

- `<filename>` is the exact uploaded name, Cisco-canonical (e.g.
  `cat9k_iosxe.17.09.04.SPA.bin`).
- `<host>` is the firmware server's `FIRMWARE_SERVER_NAME` — reachable from both
  the device management network and the Nautobot worker. No credentials in the URL.

Worker-internal (used only to **validate** an image during registration; never
stored): the worker reaches the download service directly on the Docker network:

```
http://firmware-download/images/<filename>
```

## How this repo integrates (configuration)

Set these on the **Nautobot worker** environment (the Register job reads them):

| Env var | Purpose | Default |
|---|---|---|
| `FIRMWARE_BASE_URL` | Device-facing base (plain **HTTP**); `download_url = base + filename` | **required** (no default — the job aborts if unset, unless the per-run field or a full Download URL override is given) |
| `FIRMWARE_BASE_URL_HTTPS` | HTTPS variant, used instead of the above when the job's **Use HTTPS URL** option is ticked | unset (the job aborts if the option is ticked without it) |
| `FIRMWARE_INTERNAL_URL` | Worker validation base (internal HTTP); set `""` to disable | `http://firmware-download/images/` |

The base is also overridable per run on the **Register IOS-XE Image** job
(`firmware_base_url`, or a full `download_url_override` — explicit values win
verbatim and the HTTPS toggle is ignored for them). The companion
nautobot-composer `setup.sh` writes both base-URL variables into its `.env`,
which is the `env_file` for the worker. Defaults live in
[`jobs/constants.py`](../jobs/constants.py).

## Upload + registration workflow

1. **Acquire** the image from Cisco (CCO); note Cisco's published checksum (SHA512
   preferred). **Verify** it locally.
2. **Upload** to the firmware server via the **Filebrowser UI** (`:8088`), keeping
   the canonical filename.
3. **Register the image** — run **Register IOS-XE Image**:
   - `image_file_name` = the uploaded filename
   - `software_version` = the existing version, **or** leave it blank and set
     `new_version` to create the version inline (**platform** and **version
     status** are required fields either way)
   - `device_types` = the compatible models (e.g. the C9300 types), **and/or**
     tick `default_image`. **At least one of these matters:** the upgrade job can
     only resolve an image that is mapped to the device's type, assigned directly
     to the device, or marked as the version's default — with neither set, the
     registration succeeds (with a warning) but no upgrade can use the image.
   - `image_status`
   - `expected_checksum` + `hashing_algorithm` (Cisco's published values);
     optionally tick **Verify download** (worker downloads + hashes the file)
   - run **Dry-run** first, then for real.

   The job builds the device `download_url` from `FIRMWARE_BASE_URL` + filename
   (or `FIRMWARE_BASE_URL_HTTPS` + filename with **Use HTTPS URL** ticked),
   validates the file is reachable (trying `FIRMWARE_INTERNAL_URL` first, then the
   device URL), records size + checksum, creates the `SoftwareVersion` if one
   wasn't selected, creates/updates the `SoftwareImageFile`, and maps it to the
   device types. The upgrade job then consumes `download_url`.

## TLS

> **Status: HTTPS device pulls are not yet tested or validated.** All testing to
> date uses plain HTTP. For this traffic — public, Cisco-signed images whose
> integrity is already verified independently (byte-exact size + `install add`
> signature validation) — encryption buys little, so on a locked-down management
> segment HTTPS may simply not be necessary. The notes below are the intended
> path if you do want it.

The firmware server's HTTPS cert is **self-signed by default**, and IOS-XE's
HTTPS transfer validates the server cert against the device's trustpoints —
which is why the stored `download_url` defaults to the **HTTP** endpoint on a
locked-down management VLAN. To move device pulls to HTTPS: (a) have the
firmware server present a CA-issued cert the devices trust, or (b) install the
firmware server's CA in a device trustpoint (`crypto pki trustpoint` +
`authenticate`) — then tick **Use HTTPS URL** on the Register job so new images
store the `FIRMWARE_BASE_URL_HTTPS` link.

Worker-side validation avoids the issue by using the internal **HTTP**
`firmware-download` route. If you instead validate an HTTPS URL, **Verify repo
TLS** on the Register job is **off by default** (self-signed-friendly); turn it on
when the server presents a CA-trusted cert.

## Management & lifecycle

## How checksum verification works (the Register job's three fields)

Common point of confusion — the exact mechanics:

- **When:** ONE time, at registration, and only if **Verify download** is
  ticked. Nothing is re-verified later, and nothing happens during upgrades.
- **Where:** on the **Nautobot Celery worker**. It downloads the image
  **streamed** (1 MB chunks fed straight into the hash — the file is never
  written to the Nautobot server's disk), preferring the internal
  `firmware-download` route so the transfer stays on the Docker network.
- **Never on the switch.** IOS-XE's `verify` RPC is deliberately not used (its
  results are notification-only — see the README's design choices). Device-side
  integrity during an upgrade is the byte-exact size match plus `install add`'s
  mandatory Cisco signature validation.
- **Field combinations:**
  - `Expected checksum` + `Hashing algorithm`, Verify OFF → recorded on the
    `SoftwareImageFile` verbatim, **unverified** (paste Cisco's published hash).
  - Verify ON + expected checksum → worker computes and compares; a
    **mismatch aborts the registration** (corrupt upload or wrong file).
  - Verify ON, **no** expected checksum → the computed hash is **recorded as
    the baseline**.
  - An algorithm the worker can't compute (outside Python's hashlib set) →
    warns and records the provided value without verifying.

- **Integrity, layered:** verify at download → verify at upload → (optionally)
  re-verify in the Register job → the upgrade job size-checks the copied file on
  the device → `install add` validates the image signature and aborts on a corrupt
  image.
- **Access:** device download is read-only and network/ACL-restricted
  (`FIRMWARE_ALLOWED_CIDRS` on the firmware server); the Filebrowser UI is
  authenticated and for humans only.
- **Retention:** keep the current and previous release hosted — this is the
  GUARANTEED rollback path during a soak period (a downgrade is just an upgrade-job
  run targeting the older version; leftover packages on the device are not a
  reliable rollback vehicle). Prune older files per policy. The upgrade job's *Remove inactive* option reclaims space
  **on the device**, separate from firmware-server retention.
- **Drift:** Nautobot indexes; the firmware server holds the bytes. A reconcile/
  audit job (server files ↔ Nautobot records) is deliberately deferred.

## What is intentionally NOT here

- No uploading of binaries through Nautobot (it is not a file host for ~1 GB
  blobs, and has no device-facing download endpoint).
- No credentials in `download_url` — device access is by network restriction.
- No object-store/presigned-URL flow (would require minting URLs at upgrade time);
  revisit if you move off the static firmware server.
