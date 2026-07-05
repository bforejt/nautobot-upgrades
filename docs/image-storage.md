# Image storage, upload & management

How the Cisco IOS-XE `.bin` images are stored, published, and tracked for the
upgrade job.

## Architecture: Nautobot is the index, the firmware server holds the bytes

The upgrade is **RESTCONF-only and device-initiated** — the switch pulls its own
image via the `Cisco-IOS-XE-rpc:copy` RPC from a URL we hand it. That dictates
the storage model:

```
            (metadata / index)                  (the bytes — companion stack)
   ┌────────────────────────────┐      ┌──────────────────────────────────────┐
   │ Nautobot core              │      │ "nautobot-composer" `firmware` profile │
   │  dcim.SoftwareVersion      │      │  • Filebrowser UI  :8088 (engineers)   │
   │  dcim.SoftwareImageFile    │      │  • nginx firmware-download             │
   │   • download_url ──────────┼─────▶│      :9443 https / :9080 http (devices)│
   │   • checksum + algorithm    │      │    shared volume, read-only, ACL       │
   │   • file size               │      └──────────────────────────────────────┘
   │   • mapped device types      │                 ▲              ▲
   └──────────────────────────────┘     device pull │              │ worker validates
                                          (copy RPC) │              │ (internal HTTP)
                                       ┌─────────────┴──┐   ┌───────┴──────────┐
                                       │ Catalyst 9300  │   │ Nautobot worker  │
                                       └────────────────┘   └──────────────────┘
```

- **Nautobot stores only metadata** (`SoftwareImageFile`: file name, checksum +
  algorithm, size, `download_url`, default flag, device-type map). It does not
  store the binary and is not a device-facing file server.
- **The bytes live on the companion `nautobot-composer` stack's `firmware`
  profile** (opt-in). Two services share one volume:
  - **Filebrowser** (`:8088`, authenticated) — engineers upload/manage images.
  - **nginx `firmware-download`** (`:9443` HTTPS / `:9080` HTTP, read-only) —
    serves the same files to devices, **unauthenticated but network/ACL
    restricted**. Directory listing off; GET/HEAD only; `Content-Type:
    application/octet-stream`; HEAD returns a correct `Content-Length`; byte-range
    supported.

## Download URL format

Device-facing (this is what gets stored in `download_url` and handed to the
device's `copy` RPC):

```
https://<host>:9443/images/<filename>     # default HTTPS
http://<host>:9080/images/<filename>      # HTTP fallback (locked-down VLAN)
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
| `FIRMWARE_BASE_URL` | Device-facing base; `download_url = base + filename` | **required** (no default — the job aborts if unset, unless the per-run field or a full Download URL override is given) |
| `FIRMWARE_INTERNAL_URL` | Worker validation base (internal HTTP); set `""` to disable | `http://firmware-download/images/` |

Both are also overridable per run on the **Register IOS-XE Image** job
(`firmware_base_url`, or a full `download_url_override`). Defaults live in
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

   The job builds the device `download_url` from `FIRMWARE_BASE_URL` + filename,
   validates the file is reachable (trying `FIRMWARE_INTERNAL_URL` first, then the
   device URL), records size + checksum, creates the `SoftwareVersion` if one
   wasn't selected, creates/updates the `SoftwareImageFile`, and maps it to the
   device types. The upgrade job then consumes `download_url`.

## TLS

The firmware server's HTTPS cert is **self-signed by default**. IOS-XE
`copy https:` validates the server cert against the device's trustpoints, so for
real device pulls either (a) the firmware server presents a CA-trusted cert the
devices trust, or (b) use the **HTTP** URL on a locked-down management VLAN.

Worker-side validation avoids the issue by using the internal **HTTP**
`firmware-download` route. If you instead validate an HTTPS URL, **Verify repo
TLS** on the Register job is **off by default** (self-signed-friendly); turn it on
when the server presents a CA-trusted cert.

## Management & lifecycle

- **Integrity, layered:** verify at download → verify at upload → (optionally)
  re-verify in the Register job → the upgrade job size-checks the copied file on
  the device → `install add` validates the image signature and aborts on a corrupt
  image.
- **Access:** device download is read-only and network/ACL-restricted
  (`FIRMWARE_ALLOWED_CIDRS` on the firmware server); the Filebrowser UI is
  authenticated and for humans only.
- **Retention:** keep the current and previous release (the rollback image); prune
  older files per policy. The upgrade job's *Remove inactive* option reclaims space
  **on the device**, separate from firmware-server retention.
- **Drift:** Nautobot indexes; the firmware server holds the bytes. A reconcile/
  audit job (server files ↔ Nautobot records) is deliberately deferred.

## What is intentionally NOT here

- No uploading of binaries through Nautobot (it is not a file host for ~1 GB
  blobs, and has no device-facing download endpoint).
- No credentials in `download_url` — device access is by network restriction.
- No object-store/presigned-URL flow (would require minting URLs at upgrade time);
  revisit if you move off the static firmware server.
