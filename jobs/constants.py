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

#: Substrings used to match the target filesystem's partition name in the
#: platform-software-oper data (partition names vary: flash / bootflash / ...).
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
#: The copy RPC blocks for the full image transfer (~1 GB) with no async
#: progress, so its timeout is large.
COPY_TIMEOUT = 3600

POLL_INTERVAL = 30
#: How long to wait for "install add" to finish staging the package.
ADD_TIMEOUT = 1200
#: After "install activate" the device reloads; how long to wait before it
#: starts responding to RESTCONF again.
RELOAD_INITIAL_SLEEP = 120
RELOAD_TIMEOUT = 1800

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
