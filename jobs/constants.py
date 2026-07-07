"""Tunable constants for the Cisco IOS-XE RESTCONF upgrade job.

Everything here is deliberately centralised so an operator can adjust paths,
timeouts, and safety thresholds for their environment / IOS-XE release without
editing the job logic. Several RESTCONF operational ("-oper") leaf paths and
filesystem partition names drift between IOS-XE releases and platforms; the
constants below make those easy to tweak.

RESTCONF endpoints used (verified against Cisco's published YANG models):
  * Cisco-IOS-XE-install-rpc (install / activate / install-commit / remove)
  * Cisco-IOS-XE-rpc (copy — the classic blocking transfer)
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

#: JSON keys that may carry the boot mode in install-oper data. Verified against
#: Cisco's published YANG: the leaf is 'boot-mode' (typedef install-boot-mode,
#: values install-boot-mode-{unknown,install,bundle}) under
#: install-location-information[]/oper-state; 'install-mode' is kept as a
#: fallback for releases that may name it differently. NOTE: the leaf was ADDED
#: in IOS-XE 17.5.1 (install-oper revision 2021-03-01); every supported release
#: (>= 17.12.1) has it, so assume_install_mode exists only for naming/model drift.
BOOT_MODE_KEYS = ("boot-mode", "install-mode")

#: Minimum IOS-XE release the job supports. Boundaries (verified against
#: Cisco's published YANG models for every train 16.12.1-17.11.1):
#:   * install-rpc appears 17.2.1, install-oper 17.3.1 (16.12 = hard wall);
#:   * boot-mode + sys-activity leaves appear together at 17.5.1;
#:   * the operation ledger (install-oper/-hist, op-uuid keyed) at 17.8.1;
#:   * per-file sizes become unambiguously BYTES at 17.9.1 (platform-software
#:     rev 2022-07-01) — on 17.5-17.8 the partition-content size leaf is
#:     described as KILOBYTES, which would false-abort the byte-exact copy
#:     verification, so those trains are refused.
#: 17.9.1 is therefore the lowest release where the PRIMARY tier (ledger
#: tracking + sys-activity gate + byte-exact verify) is fully model-complete.
#: Hardware-validated baseline remains 17.15.x; run one supervised upgrade per
#: new train before fleet use.
MIN_IOSXE_VERSION = (17, 9, 1)

# --- RESTCONF resource paths (relative to /restconf/) ------------------------

#: device-hardware-data -> device-hardware -> device-system-data -> software-version
DATA_DEVICE_SYSTEM = (
    "data/Cisco-IOS-XE-device-hardware-oper:device-hardware-data/device-hardware/device-system-data"
)
DATA_INSTALL_OPER = "data/Cisco-IOS-XE-install-oper:install-oper-data"

#: Hardware inventory (stack member roster: hw-type-chassis entries with
#: hw-dev-index + serial-number, verified against the 17.15 YANG).
DATA_DEVICE_INVENTORY = (
    "data/Cisco-IOS-XE-device-hardware-oper:device-hardware-data/device-hardware/device-inventory"
)

#: Filesystem data. Partitions carry name + total-size + used-size in KILOBYTES;
#: file entries carry full-path + file-size in BYTES (>= 17.9 — the model's
#: 2022-07-01 revision fixed the units description). The exact partition
#: name for flash on a given platform may differ — see TARGET_FS_NAMES.
DATA_Q_FILESYSTEM = "data/Cisco-IOS-XE-platform-software-oper:cisco-platform-software/q-filesystem"

#: Classic blocking copy RPC (two decades of production miles; chosen over the
#: async xcopy after a real 17.15.05 silently broke xcopy transfers while this
#: path kept working with the same URL). The job runs it in a worker thread so
#: the on-device file size can still be polled for progress reporting.
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
#: Overall budget (seconds) for the copy: the blocking RPC's HTTP timeout in the
#: worker thread, and the watcher's deadline for the whole transfer.
COPY_TIMEOUT = 3600

POLL_INTERVAL = 30
#: How long to wait for "install add" to finish staging the package. The target
#: version appears in install-oper as soon as the add STARTS, so the gate waits
#: for an add-complete state (added/inactive or beyond), not mere presence.
ADD_TIMEOUT = 1200

#: How long to wait after the reload for EVERY pre-upgrade stack member to
#: rejoin (members can come up staggered) before refusing to commit.
MEMBER_CHECK_TIMEOUT = 300

#: How long to poll for install-oper to report the target version COMMITTED
#: after the commit RPC. The RPC returns before the engine finishes (a real
#: 17.15.4 showed provisioned-uncommitted for a few seconds), so a single
#: immediate read false-warns.
COMMIT_CONFIRM_TIMEOUT = 300

#: How long to wait for the activation to actually START after the activate RPC
#: (state turns activated/uncommitted, or the device drops offline to reload).
#: The RPC returns 2xx even when the install engine rejects it — e.g. 'add in
#: progress' — so the state change is the real gate.
ACTIVATE_START_TIMEOUT = 600

#: Engine-activity gate before every install-engine write. The oper-state
#: 'sys-activity' leaf (typedef install-system-activity: install-no-activity /
#: install-install-in-progress / install-issu-in-progress) is the engine's own
#: busy indicator; requests that arrive while it is busy are SILENTLY dropped
#: (field-verified: an activate landing inside the add's ~66s add-postchk phase
#: never started an operation). Wait for every member to report no-activity.
ENGINE_IDLE_TIMEOUT = 300

#: Fallback settle delay before activate when sys-activity is not reported by
#: the release AND the add could not be ledger-confirmed (the add's post-check
#: phase measured ~66s on a real 17.15.x; padded for stacks). Used ONLY when
#: no positive signal is available — state over inference, timers as last resort.
ACTIVATE_SETTLE_DELAY = 120

#: How many ledger polls with our op-uuid absent before concluding the engine
#: dropped the request (activate → re-send the same request) or the release
#: does not populate the operation ledger (add/commit → legacy state inference).
LEDGER_ABSENT_POLLS = 3
#: After "install activate" the device reloads; how long to wait before it
#: starts responding to RESTCONF again.
RELOAD_INITIAL_SLEEP = 120
#: Overall budget for the device to (a) come back online AND (b) report the
#: target version after activate/reload. The booted-version read is polled within
#: this window so a slow-to-converge control plane is not falsely failed.
RELOAD_TIMEOUT = 1800

#: NOTE: the activate RPC deliberately does NOT send auto-abort-timer-val, and
#: sends issu=false explicitly — a real 17.15.4 fatally failed activation on an
#: "ISSU compatibility check" with the timer leaf supplied. The platform's
#: default auto-abort timer applies instead and is verified after reload.

# --- Safety thresholds ------------------------------------------------------

#: Require at least this multiple of the image size free on the target
#: filesystem before copying. Cisco's Catalyst 9000 upgrade guidance recommends
#: roughly 2x the image size of free space for an install-mode upgrade.
SPACE_HEADROOM_FACTOR = 2.0

#: Fallback minimum free bytes to require when the image size is unknown in
#: Nautobot (~2 GB; typical C9300 images are 800 MB - 1.2 GB).
SPACE_FALLBACK_MIN_BYTES = 2_000_000_000

#: Tolerance (bytes) when comparing the on-device file size to the expected size.
#: Both sides are byte-exact (device reports file sizes in bytes; the Register
#: job records the server's Content-Length), so demand an exact match — this also
#: closes the window where a still-growing file could pass near the target.
SIZE_MATCH_TOLERANCE_BYTES = 0

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
#: HTTP port (e.g. http://192.0.2.10:9080/images/ — the companion stack writes
#: this into .env, plain HTTP by default because device HTTPS clients validate
#: the server cert against their trustpoints and reject the self-signed one).
#: REQUIRED: set this env var on the worker, or use the per-run field / a full
#: Download URL override. There is intentionally NO default — the job aborts
#: rather than guess a host and store a download_url devices can't reach.
FIRMWARE_BASE_URL_ENV = "FIRMWARE_BASE_URL"

#: HTTPS variant of the base URL (e.g. https://192.0.2.10:9443/images/), used
#: instead of FIRMWARE_BASE_URL when the Register job's "Use HTTPS URL" option
#: is selected — appropriate once the firmware server presents a CA-issued
#: certificate the devices trust. The two are separate variables because the
#: HTTPS endpoint differs in scheme AND port, so neither can be derived from
#: the other. The companion stack writes both into .env.
FIRMWARE_BASE_URL_HTTPS_ENV = "FIRMWARE_BASE_URL_HTTPS"

#: INTERNAL URL the Celery worker uses to VALIDATE an image — it reaches the nginx
#: "firmware-download" service directly on the Docker network (plain HTTP, no cert
#: hassles). The stored device URL still uses FIRMWARE_BASE_URL. Set the
#: FIRMWARE_INTERNAL_URL env var to change it, or to "" to disable internal
#: validation (then the device URL is validated directly).
FIRMWARE_INTERNAL_URL_ENV = "FIRMWARE_INTERNAL_URL"
FIRMWARE_INTERNAL_URL_DEFAULT = "http://firmware-download/images/"
