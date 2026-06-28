"""Tunable constants for the Cisco IOS-XE RESTCONF upgrade job.

Everything here is deliberately centralised so an operator can adjust paths,
timeouts, and safety thresholds for their environment / IOS-XE release without
editing the job logic. Several RESTCONF operational ("-oper") leaf paths and
filesystem partition names drift between IOS-XE releases and platforms; the
constants below make those easy to tweak.

RESTCONF endpoints used (verified against Cisco's published YANG models):
  * Cisco-IOS-XE-install-rpc (install / activate / install-commit / remove)
  * Cisco-IOS-XE-rpc (copy)
  * Cisco-IOS-XE-device-hardware-oper / Cisco-IOS-XE-install-oper (state reads)
  * Cisco-IOS-XE-platform-software-oper (filesystem free space / file sizes)
"""

# --- Connectivity -----------------------------------------------------------

#: Default RESTCONF port on IOS-XE (HTTPS).
RESTCONF_PORT = 443

#: Network devices almost always present a self-signed RESTCONF certificate, so
#: TLS verification is disabled by default. Set True (and distribute the device
#: CA) for a stricter posture.
VERIFY_TLS = False

#: Writable filesystem on a Catalyst 9300 (the C9000 upgrade guide uses flash:).
TARGET_FS = "flash:"

#: Partition name(s) of the target filesystem in the platform-software-oper data.
#: Matched by exact name OR a stack-member suffix (e.g. "flash", "flash-1",
#: "flash:1") — NOT as a loose substring, so "bootflash"/"usbflash" never match.
#: Add aliases here if a release/platform names the writable flash differently.
TARGET_FS_NAMES = ("flash",)

# --- Version gating ---------------------------------------------------------

#: Minimum IOS-XE release that exposes the Cisco-IOS-XE-install-rpc /
#: install-oper YANG models. Below this the install workflow is simply not
#: available over RESTCONF, so the job refuses to proceed. (Verified against the
#: published Cisco YANG models: install-rpc first appears in 17.2.1, install-oper
#: in 17.3.1; neither exists on 16.12.x.)
MIN_IOSXE_VERSION = (17, 3, 1)

# --- RESTCONF resource paths (relative to /restconf/) ------------------------

#: device-hardware-data -> device-hardware -> device-system-data -> software-version
DATA_DEVICE_SYSTEM = (
    "data/Cisco-IOS-XE-device-hardware-oper:device-hardware-data"
    "/device-hardware/device-system-data"
)
DATA_INSTALL_OPER = "data/Cisco-IOS-XE-install-oper:install-oper-data"

#: Filesystem data (partitions carry name + total-size + used-size in KILOBYTES,
#: and image files carry full-path + file-size in KILOBYTES). The exact partition
#: name for flash on a given platform may differ — see TARGET_FS_NAMES.
DATA_Q_FILESYSTEM = "data/Cisco-IOS-XE-platform-software-oper:cisco-platform-software/q-filesystem"

OP_COPY = "operations/Cisco-IOS-XE-rpc:copy"
OP_INSTALL = "operations/Cisco-IOS-XE-install-rpc:install"
OP_ACTIVATE = "operations/Cisco-IOS-XE-install-rpc:activate"
OP_COMMIT = "operations/Cisco-IOS-XE-install-rpc:install-commit"
OP_REMOVE = "operations/Cisco-IOS-XE-install-rpc:remove"

# --- Timeouts / polling (seconds) -------------------------------------------

GET_TIMEOUT = 30
RPC_TIMEOUT = 120
#: Retries for the q-filesystem read (free-space / copied-file-size) so a transient
#: blip right after a long copy isn't mistaken for "no data".
QFS_READ_RETRIES = 3
#: The copy RPC blocks for the full image transfer (~1 GB) with no async
#: progress, so its timeout is large.
COPY_TIMEOUT = 3600

POLL_INTERVAL = 30
#: How long to wait for "install add" to finish staging the package.
ADD_TIMEOUT = 1200
#: After "install activate" the device reloads; how long to wait before it
#: starts responding to RESTCONF again.
RELOAD_INITIAL_SLEEP = 120
#: Overall budget for the device to (a) come back online AND (b) report the
#: target version after activate/reload. The booted-version read is polled within
#: this window so a slow-to-converge control plane is not falsely failed.
RELOAD_TIMEOUT = 1800

#: Auto-abort (rollback) timer in MINUTES, armed explicitly on "install activate".
#: If we cannot confirm the new image after reload we do NOT commit, and the
#: device reverts when this timer expires. Must exceed RELOAD_TIMEOUT comfortably.
#: (The RESTCONF leaf name is research-derived — verify against your release.)
AUTO_ABORT_MINUTES = 60

# --- Safety thresholds ------------------------------------------------------

#: Require at least this multiple of the image size free on the target
#: filesystem before copying. Cisco's Catalyst 9000 upgrade guidance recommends
#: roughly 2x the image size of free space for an install-mode upgrade.
SPACE_HEADROOM_FACTOR = 2.0

#: Fallback minimum free bytes to require when the image size is unknown in
#: Nautobot (~2 GB; typical C9300 images are 800 MB - 1.2 GB).
SPACE_FALLBACK_MIN_BYTES = 2_000_000_000

#: Tolerance (bytes) when comparing the on-device file size to the expected size.
#: The device reports sizes in KB, so allow one KB of rounding.
SIZE_MATCH_TOLERANCE_BYTES = 1024

# --- Image repository / Register Image job ----------------------------------

#: Default for the Register job's worker-side HTTPS validation. The firmware
#: server's device-facing cert is self-signed by default, so verification is OFF;
#: turn it on (per run, or here) when the server presents a CA-trusted cert. (The
#: preferred internal HTTP validation route ignores this entirely.)
REPO_VERIFY_TLS = False

#: Timeout (seconds) for the reachability/size HEAD request to the repository.
REPO_HEAD_TIMEOUT = 30

#: Timeout (seconds) for downloading the full image when hash verification is
#: requested (the worker streams the whole file to compute its digest).
REPO_DOWNLOAD_TIMEOUT = 3600

#: Streaming read size (bytes) when hashing a downloaded image.
REPO_CHUNK_SIZE = 1 << 20

#: Hashing algorithms the Register Image job can compute locally (a subset of
#: core SoftwareImageFileHashingAlgorithmChoices that Python's hashlib supports
#: directly). Others can still be recorded, just not verified in-job.
HASHLIB_SUPPORTED = ("md5", "sha1", "sha224", "sha256", "sha384", "sha512")

# --- Firmware server integration (companion "nautobot-composer" stack) -------
#
# Firmware images are hosted by the companion stack's opt-in `firmware` profile:
# a Filebrowser UI (engineers upload) + a read-only nginx "firmware-download"
# service serving the same volume to devices. See docs/image-storage.md.

#: DEVICE-FACING base URL stored in SoftwareImageFile.download_url as
#: "<base>/<filename>". Must be reachable from the device management network AND
#: the Nautobot worker, and matches the firmware server's FIRMWARE_SERVER_NAME +
#: HTTPS port (e.g. https://firmware.lab.example:9443/images/). REQUIRED: set this
#: env var on the worker, or use the per-run field / a full Download URL override.
#: There is intentionally NO default — the job aborts rather than guess a host and
#: store a download_url devices can't reach.
FIRMWARE_BASE_URL_ENV = "FIRMWARE_BASE_URL"

#: INTERNAL URL the Celery worker uses to VALIDATE an image — it reaches the nginx
#: "firmware-download" service directly on the Docker network (plain HTTP, no cert
#: hassles). The stored device URL still uses FIRMWARE_BASE_URL. Set the
#: FIRMWARE_INTERNAL_URL env var to change it, or to "" to disable internal
#: validation (then the device URL is validated directly).
FIRMWARE_INTERNAL_URL_ENV = "FIRMWARE_INTERNAL_URL"
FIRMWARE_INTERNAL_URL_DEFAULT = "http://firmware-download/images/"
