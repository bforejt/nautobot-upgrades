"""Cisco IOS-XE (Catalyst 9300) software upgrade Job — RESTCONF only.

This Job upgrades one or more Cisco IOS-XE devices to a target software version
using INSTALL mode, driven entirely over RESTCONF. It behaves like a cautious
engineer: every step is a PASS/FAIL gate, and the job stops on the first failed
gate for a device rather than pushing forward.

Scope (kept deliberately small):
  * IOS-XE Catalyst 9300, devices currently running >= 17.3.1 (the floor where
    the install RESTCONF models exist). Lower releases are refused with guidance.
  * Reads target version + image metadata from CORE Nautobot
    (dcim.SoftwareVersion / dcim.SoftwareImageFile). No Device Lifecycle app
    dependency.
  * Credentials come from the device's core Secrets Group (or an override).

Upgrade flow (per device):
  0. Resolve credentials + RESTCONF reachability
  1. Pre-flight gates: current version, install-mode, version floor, image
     resolution + compatibility, free-space
  2. Copy the image (device-initiated) and verify it arrived intact
  3. install add  ->  install activate (auto-rollback timer armed)  ->  reload
  4. Reconnect, verify the new version actually booted  ->  install commit
  5. Post-checks + sync Nautobot's Device.software_version
  6. Optional: install remove inactive (off by default)

NOTE: This project is brand new and has NOT been validated against real
hardware. Treat the exact RESTCONF payloads/paths as research-derived and verify
in a lab before production use. Always run with Dry-run first.
"""

from __future__ import annotations

import math
import re
import time
import uuid as uuid_lib

from django.db import transaction
from nautobot.apps.jobs import BooleanVar, DryRunVar, Job, MultiObjectVar, ObjectVar
from nautobot.dcim.models import Device, SoftwareImageFile, SoftwareVersion
from nautobot.extras.choices import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)
from nautobot.extras.models import SecretsGroup

from . import constants as C
from .restconf import RestconfClient, RestconfError

name = "IOS-XE Upgrades"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class UpgradeAbort(Exception):
    """A safety gate failed; abort this device's upgrade (not the whole job)."""


def _version_tuple(text):
    """Extract a (major, minor, patch) tuple from any IOS-XE version string.

    Handles both '17.3.1' and Cisco's zero-padded '17.09.04' forms, and full
    banner strings like 'Cisco IOS-XE Software, Version 17.06.03'.
    """
    match = _VERSION_RE.search(str(text or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


class IOSXEUpgrade(Job):
    """Upgrade Cisco IOS-XE Catalyst 9300 devices over RESTCONF (install mode)."""

    devices = MultiObjectVar(
        model=Device,
        required=True,
        description="One or more target devices to upgrade.",
    )
    target_version = ObjectVar(
        model=SoftwareVersion,
        required=True,
        description=(
            "The Software Version (core dcim.SoftwareVersion) to upgrade to. Its "
            "Software Image File must have a download URL and, ideally, a file size."
        ),
    )
    secrets_group = ObjectVar(
        model=SecretsGroup,
        required=False,
        description=(
            "Optional override for device credentials. Defaults to the device's "
            "assigned Secrets Group."
        ),
    )
    remove_inactive = BooleanVar(
        default=False,
        description=(
            "After a successful commit, run 'install remove inactive' to reclaim "
            "space. Off by default so the previous image is kept for a soak period."
        ),
    )
    debug = BooleanVar(
        default=False,
        description="Verbose logging of every RESTCONF request/response.",
    )
    dryrun = DryRunVar(
        description=(
            "Run all read-only pre-flight gates and report what WOULD happen, "
            "without copying, installing, or modifying anything."
        ),
    )

    class Meta:
        name = "Cisco IOS-XE Upgrade (RESTCONF)"
        description = (
            "Conservative, gate-driven IOS-XE install-mode upgrade for Catalyst "
            "9300 devices, driven entirely over RESTCONF. Requires devices running "
            "IOS-XE >= 17.3.1 with RESTCONF enabled."
        )
        has_sensitive_variables = False
        dryrun_default = True
        soft_time_limit = 5400
        time_limit = 7200

    # ------------------------------------------------------------------ run --

    def run(self, *, devices, target_version, secrets_group, remove_inactive, debug, dryrun):
        # self.logger.success() exists only on Nautobot >= 2.4; fall back to info.
        log_success = getattr(self.logger, "success", self.logger.info)
        results = {}
        self.logger.info(
            "Starting IOS-XE upgrade to **%s** for %d device(s)%s.",
            target_version,
            len(devices),
            " (DRY-RUN)" if dryrun else "",
        )
        for device in devices:
            try:
                summary = self._upgrade_device(
                    device, target_version, secrets_group, remove_inactive, debug, dryrun
                )
                results[device.name] = summary
                log_success(summary, extra={"object": device})
            except UpgradeAbort as exc:
                results[device.name] = f"ABORTED: {exc}"
                self.logger.error("Upgrade aborted: %s", exc, extra={"object": device})
            except RestconfError as exc:
                hint = _auth_hint(exc.status_code)
                results[device.name] = f"RESTCONF error: {exc}{hint}"
                self.logger.error("RESTCONF error: %s%s", exc, hint, extra={"object": device})
            except Exception as exc:  # noqa: BLE001 - surface anything unexpected
                results[device.name] = f"UNEXPECTED error: {exc}"
                self.logger.error("Unexpected error: %s", exc, extra={"object": device})
        return results

    # ----------------------------------------------------------- orchestrate --

    def _upgrade_device(
        self, device, target_version, override_group, remove_inactive, debug, dryrun
    ):
        log = {"object": device}

        # -- 0. Credentials + reachability -----------------------------------
        host = self._device_host(device)
        username, password = self._credentials(device, override_group)
        client = RestconfClient(
            host, username, password, logger=self.logger, log_object=device, debug=debug
        )
        self._check_reachable(client, host)
        self.logger.info("RESTCONF reachable and authenticated at %s.", host, extra=log)

        # -- 1. Pre-flight gates ---------------------------------------------
        current = self._current_version(client)
        self.logger.info("Current version: **%s**.", current or "unknown", extra=log)

        target_str = target_version.version
        if _version_tuple(current) and _version_tuple(current) == _version_tuple(target_str):
            return f"Already running target version {target_str}; nothing to do."

        self._gate_version_floor(current, log)
        self._gate_install_mode(client, log)

        image = self._resolve_image(device, target_version, log)
        self._gate_free_space(client, image, log)

        if dryrun:
            return (
                f"DRY-RUN ok: would copy '{image.download_url}' to "
                f"{C.TARGET_FS}{image.image_file_name} and install {target_str}. "
                "All pre-flight gates passed."
            )

        # -- 2. Transfer + integrity -----------------------------------------
        self._copy_image(client, image, log)
        self._verify_image(client, image, log)

        # -- 3. install add / activate / reload ------------------------------
        op_uuid = str(uuid_lib.uuid4())
        self._install_add(client, image, op_uuid, log)
        self._install_activate(client, image, op_uuid, log)
        self._wait_for_reload(client, log)

        # -- 4. Verify booted, then commit -----------------------------------
        booted = self._current_version(client)
        self.logger.info("Post-reload version: **%s**.", booted or "unknown", extra=log)
        if not _version_tuple(booted) or _version_tuple(booted) != _version_tuple(target_str):
            raise UpgradeAbort(
                f"Device did not boot the target version (got {booted!r}, expected "
                f"{target_str}). NOT committing — the auto-rollback timer will revert "
                "the device to the previous image."
            )
        self._install_commit(client, op_uuid, log)

        # -- 5. Post-checks + sync Nautobot ----------------------------------
        self._verify_committed(client, log)
        self._sync_nautobot(device, target_version, log)

        # -- 6. Optional cleanup ---------------------------------------------
        if remove_inactive:
            self._remove_inactive(client, op_uuid, log)

        return f"Upgraded and committed to {target_str}."

    # -------------------------------------------------------- helpers: setup --

    @staticmethod
    def _device_host(device):
        primary = device.primary_ip4 or device.primary_ip
        if not primary:
            raise UpgradeAbort("Device has no primary IP address.")
        return str(primary.host)

    def _credentials(self, device, override_group):
        group = override_group or device.secrets_group
        if not group:
            raise UpgradeAbort(
                "No Secrets Group on the device and no override provided; cannot "
                "obtain RESTCONF credentials."
            )
        username = self._secret(group, device, SecretsGroupSecretTypeChoices.TYPE_USERNAME)
        password = self._secret(group, device, SecretsGroupSecretTypeChoices.TYPE_PASSWORD)
        return username, password

    @staticmethod
    def _secret(group, device, secret_type):
        # Try the most specific access types first; fall back gracefully across
        # Nautobot versions that may not define a RESTCONF access type.
        candidates = [
            getattr(SecretsGroupAccessTypeChoices, attr, None)
            for attr in ("TYPE_RESTCONF", "TYPE_HTTP", "TYPE_REST", "TYPE_GENERIC")
        ]
        for access_type in [c for c in candidates if c]:
            try:
                return group.get_secret_value(
                    access_type=access_type, secret_type=secret_type, obj=device
                )
            except Exception:  # noqa: BLE001 - wrong access type / missing secret
                continue
        raise UpgradeAbort(
            f"Could not retrieve a '{secret_type}' secret from Secrets Group "
            f"'{group}' for any of the RESTCONF/HTTP/Generic access types."
        )

    @staticmethod
    def _check_reachable(client, host):
        """Confirm RESTCONF is reachable AND the credentials authenticate.

        Distinguishes authentication (401) and authorization/privilege (403)
        failures from plain connectivity so the Job Result is actionable. A
        successful read proves authentication only; authorization to run the
        install operations is exercised later (and a 403 there is classified the
        same way) — the account must be privilege 15 / have install authorization.
        """
        try:
            client.get(C.DATA_DEVICE_SYSTEM, ok_404=False)
        except RestconfError as exc:
            status = exc.status_code
            if status == 401:
                raise UpgradeAbort(
                    f"RESTCONF authentication failed (HTTP 401) at {host}. Check the "
                    "Secrets Group username/password (RESTCONF access type)."
                ) from exc
            if status == 403:
                raise UpgradeAbort(
                    f"RESTCONF authorization failed (HTTP 403) at {host}. The account "
                    "authenticated but lacks rights — it must be privilege 15 (or "
                    "have exec authorization for install/copy)."
                ) from exc
            raise UpgradeAbort(
                f"RESTCONF not reachable at https://{host}:{C.RESTCONF_PORT}/restconf "
                f"({exc}). Check connectivity and that 'restconf' + 'ip http "
                "secure-server' are enabled."
            ) from exc

    # --------------------------------------------------- helpers: read state --

    def _current_version(self, client):
        data = client.get(C.DATA_DEVICE_SYSTEM, ok_404=True) or {}
        system = data.get("Cisco-IOS-XE-device-hardware-oper:device-system-data", {})
        return system.get("software-version")

    def _gate_version_floor(self, current, log):
        current_tuple = _version_tuple(current)
        floor = ".".join(str(p) for p in C.MIN_IOSXE_VERSION)
        if not current_tuple:
            raise UpgradeAbort(
                f"Could not determine the running IOS-XE version (got {current!r})."
            )
        if current_tuple < C.MIN_IOSXE_VERSION:
            raise UpgradeAbort(
                f"Running version {current} is below {floor}. RESTCONF-driven "
                "install is unavailable on this release; upgrade out of band first."
            )
        self.logger.info("Version floor gate passed (>= %s).", floor, extra=log)

    def _gate_install_mode(self, client, log):
        data = client.get(C.DATA_INSTALL_OPER, ok_404=True)
        if not data:
            raise UpgradeAbort(
                "Could not read Cisco-IOS-XE-install-oper data; RESTCONF may lack "
                "the install model. Refusing to run install commands."
            )
        # The install-mode enum is install-mode-{bundle,install,install-bundle,...}.
        # Presence of the container alone is NOT proof of install mode.
        mode = _find_value(data, "install-mode")
        state = str(mode).lower().rsplit("install-mode-", 1)[-1] if mode else ""
        if state in ("install", "install-bundle"):
            self.logger.info("Install-mode gate passed (%s).", mode, extra=log)
        elif state == "bundle":
            raise UpgradeAbort(
                "Device is in BUNDLE mode, which this job does not support. Convert "
                "it to INSTALL mode (boot flash:packages.conf) first."
            )
        else:
            self.logger.warning(
                "Could not determine boot mode from install-oper (got %r); "
                "proceeding on the assumption of INSTALL mode (the C9300 default). "
                "Verify with 'show version'.", mode, extra=log,
            )

    def _resolve_image(self, device, target_version, log):
        # Prefer an image explicitly mapped to this device's device-type for the
        # target version (core's compatibility map); otherwise fall back to the
        # version's default image.
        dt_images = list(
            device.device_type.software_image_files.filter(software_version=target_version)
        )
        image = self._pick_image(dt_images)
        if image is None:
            all_images = list(SoftwareImageFile.objects.filter(software_version=target_version))
            image = self._pick_image(all_images, require_default=True)
            if image is not None:
                self.logger.warning(
                    "No image mapped to device-type '%s' for %s; using default image "
                    "'%s'. Verify it is correct for this platform.",
                    device.device_type, target_version, image.image_file_name, extra=log,
                )
        if image is None:
            raise UpgradeAbort(
                f"No Software Image File found for version {target_version} that is "
                f"compatible with device-type '{device.device_type}'."
            )
        if not image.image_file_name:
            raise UpgradeAbort(f"Image '{image}' has no image file name set in Nautobot.")
        if not image.download_url:
            raise UpgradeAbort(
                f"Image '{image.image_file_name}' has no download URL; the device "
                "needs a URL to pull the image from."
            )
        self.logger.info(
            "Resolved image '%s' (%s).", image.image_file_name, image.download_url, extra=log
        )
        return image

    @staticmethod
    def _pick_image(images, require_default=False):
        if not images:
            return None
        for image in images:
            if image.default_image:
                return image
        return None if require_default else images[0]

    def _gate_free_space(self, client, image, log):
        free = self._read_free_space(client)
        if free is None:
            raise UpgradeAbort(
                "Could not confirm free space on the target filesystem over "
                "RESTCONF. The partition name or path may differ on this release / "
                "platform — adjust TARGET_FS_NAMES / DATA_Q_FILESYSTEM in "
                "constants.py. Refusing to copy without confirming space."
            )
        size = image.image_file_size
        if size:
            needed = math.ceil(size * C.SPACE_HEADROOM_FACTOR)
            label = f"{size} bytes x{C.SPACE_HEADROOM_FACTOR} headroom"
        else:
            needed = C.SPACE_FALLBACK_MIN_BYTES
            label = f"{needed} bytes (image size unknown in Nautobot)"
            self.logger.warning(
                "Image file size not set in Nautobot; using fallback space "
                "requirement.", extra=log,
            )
        if free < needed:
            raise UpgradeAbort(
                f"Insufficient free space: {free} bytes free, need {needed} "
                f"({label}). Run 'install remove inactive' or clean up flash."
            )
        self.logger.info(
            "Free-space gate passed (%s bytes free, need %s).", free, needed, extra=log
        )

    @staticmethod
    def _read_free_space(client):
        try:
            data = client.get(C.DATA_Q_FILESYSTEM, ok_404=True)
        except RestconfError:
            return None
        if not data:
            return None
        return _free_bytes_for_fs(data, C.TARGET_FS_NAMES)

    # ------------------------------------------------- helpers: device writes --

    def _copy_image(self, client, image, log):
        dest = f"{C.TARGET_FS}{image.image_file_name}"
        self.logger.info("Copying image to %s (this can take several minutes)...", dest, extra=log)
        payload = {
            "Cisco-IOS-XE-rpc:input": {
                "source-drop-node-name": image.download_url,
                "destination-drop-node-name": dest,
            }
        }
        client.post_rpc(C.OP_COPY, payload, timeout=C.COPY_TIMEOUT)
        self.logger.info("Copy completed.", extra=log)

    def _verify_image(self, client, image, log):
        """Confirm the copied image arrived intact.

        The on-device cryptographic hash verify RPC (Cisco-IOS-XE-verify-rpc) is
        asynchronous — its synchronous response carries no pass/fail — so we do
        NOT rely on it. Instead we confirm the on-device file size matches the
        expected size (catches truncated/incomplete transfers) and lean on
        'install add', which validates the image's digital signature and aborts on
        a corrupt or untrusted image.
        """
        expected = image.image_file_size
        if not expected:
            self.logger.warning(
                "Image file size not set in Nautobot; cannot size-check the "
                "transfer. Relying on 'install add' image signature validation. Set "
                "a file size in Nautobot for a stricter pre-install gate.", extra=log,
            )
            return
        on_device = self._read_file_size(client, image.image_file_name)
        if on_device is None:
            self.logger.warning(
                "Could not read the copied file's size over RESTCONF; relying on "
                "'install add' signature validation to catch a bad transfer.",
                extra=log,
            )
            return
        if abs(on_device - expected) > C.SIZE_MATCH_TOLERANCE_BYTES:
            raise UpgradeAbort(
                f"Copied image size {on_device} bytes != expected {expected} bytes; "
                "the transfer was incomplete or corrupt."
            )
        self.logger.info("Image size verified (%s bytes).", on_device, extra=log)

    @staticmethod
    def _read_file_size(client, image_file_name):
        try:
            data = client.get(C.DATA_Q_FILESYSTEM, ok_404=True)
        except RestconfError:
            return None
        if not data:
            return None
        return _file_size_bytes(data, image_file_name)

    def _install_add(self, client, image, op_uuid, log):
        path = f"{C.TARGET_FS}{image.image_file_name}"
        self.logger.info("install add %s ...", path, extra=log)
        payload = {"Cisco-IOS-XE-install-rpc:input": {"uuid": op_uuid, "path": path}}
        client.post_rpc(C.OP_INSTALL, payload, timeout=C.RPC_TIMEOUT)
        self._wait_for_added(client, log)

    def _wait_for_added(self, client, log):
        deadline = time.monotonic() + C.ADD_TIMEOUT
        while time.monotonic() < deadline:
            data = client.get(C.DATA_INSTALL_OPER, ok_404=True) or {}
            if "install-state-added" in str(data).lower():
                self.logger.info("install add staged.", extra=log)
                return
            time.sleep(C.POLL_INTERVAL)
        # Don't hard-fail on polling ambiguity — install-oper shape drifts between
        # releases. Warn and proceed; activate fails loudly if add never completed.
        self.logger.warning(
            "Could not positively confirm 'install add' completion from "
            "install-oper within %ss; proceeding to activate.", C.ADD_TIMEOUT, extra=log,
        )

    def _install_activate(self, client, image, op_uuid, log):
        self.logger.info(
            "install activate (the device will reload; auto-rollback timer armed)...", extra=log
        )
        # The 'activate' RPC has a mandatory choice (version/path/name); supply the
        # image path. The device arms its default ~120-minute auto-abort timer,
        # which rolls back if we do not commit after the device returns.
        payload = {
            "Cisco-IOS-XE-install-rpc:input": {
                "uuid": op_uuid,
                "path": f"{C.TARGET_FS}{image.image_file_name}",
            }
        }
        client.post_rpc(C.OP_ACTIVATE, payload, timeout=C.RPC_TIMEOUT, tolerate_disconnect=True)

    def _wait_for_reload(self, client, log):
        self.logger.info("Waiting for the device to reload...", extra=log)
        time.sleep(C.RELOAD_INITIAL_SLEEP)
        deadline = time.monotonic() + C.RELOAD_TIMEOUT
        while time.monotonic() < deadline:
            if client.ping():
                self.logger.info("Device is back online.", extra=log)
                return
            time.sleep(C.POLL_INTERVAL)
        raise UpgradeAbort(
            f"Device did not return within {C.RELOAD_TIMEOUT}s after activate. The "
            "auto-rollback timer should revert it to the previous image; NOT "
            "committing."
        )

    def _install_commit(self, client, op_uuid, log):
        self.logger.info("install commit (making the new image permanent)...", extra=log)
        payload = {"Cisco-IOS-XE-install-rpc:input": {"uuid": op_uuid}}
        client.post_rpc(C.OP_COMMIT, payload, timeout=C.RPC_TIMEOUT)

    def _verify_committed(self, client, log):
        data = client.get(C.DATA_INSTALL_OPER, ok_404=True) or {}
        if "install-state-committed" in str(data).lower():
            self.logger.info("Commit confirmed via install-oper.", extra=log)
        else:
            self.logger.warning(
                "Could not positively confirm committed state from install-oper; "
                "verify with 'show install summary'.", extra=log,
            )

    def _remove_inactive(self, client, op_uuid, log):
        self.logger.info("install remove inactive (reclaiming space)...", extra=log)
        # The 'inactive' leaf may be named 'remove-use-inactive' on some releases;
        # this step is optional and non-fatal.
        payload = {"Cisco-IOS-XE-install-rpc:input": {"uuid": op_uuid, "inactive": True}}
        try:
            client.post_rpc(C.OP_REMOVE, payload, timeout=C.RPC_TIMEOUT)
            self.logger.info("Inactive images removed.", extra=log)
        except RestconfError as exc:
            self.logger.warning("remove inactive failed (non-fatal): %s", exc, extra=log)

    def _sync_nautobot(self, device, target_version, log):
        with transaction.atomic():
            device.software_version = target_version
            device.validated_save()
        self.logger.info(
            "Updated Nautobot Device.software_version to %s.", target_version, extra=log
        )


# --------------------------------------------------------- module utilities --


def _auth_hint(status_code):
    """A human hint appended to RESTCONF errors for auth/authorization failures."""
    if status_code == 401:
        return " (authentication failed — check the Secrets Group credentials)"
    if status_code == 403:
        return (
            " (authorization failed — the account must be privilege 15 / have "
            "install authorization)"
        )
    return ""


def _find_value(data, key):
    """Return the first scalar value stored under ``key`` anywhere in ``data``."""
    if isinstance(data, dict):
        for found_key, value in data.items():
            if found_key == key and isinstance(value, (str, int, float)):
                return value
            found = _find_value(value, key)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_value(item, key)
            if found is not None:
                return found
    return None


def _find_partitions(data):
    """Collect every filesystem-partition dict (one carrying total-size + used-size)."""
    found = []
    if isinstance(data, dict):
        if "total-size" in data and "used-size" in data:
            found.append(data)
        for value in data.values():
            found.extend(_find_partitions(value))
    elif isinstance(data, list):
        for item in data:
            found.extend(_find_partitions(item))
    return found


def _partition_free(partition):
    """Free bytes of one partition (q-filesystem reports sizes in kilobytes)."""
    try:
        total = int(partition["total-size"])
        used = int(partition["used-size"])
    except (KeyError, TypeError, ValueError):
        return None
    return (total - used) * 1024


def _free_bytes_for_fs(data, fs_names):
    """Free bytes on the target filesystem from q-filesystem data (KB -> bytes)."""
    partitions = _find_partitions(data)
    if not partitions:
        return None
    # Prefer a partition whose name matches the target filesystem.
    for partition in partitions:
        partition_name = str(partition.get("name", "")).lower()
        if any(fs in partition_name for fs in fs_names):
            free = _partition_free(partition)
            if free is not None:
                return free
    # Fall back only when there is exactly one partition to choose from.
    if len(partitions) == 1:
        return _partition_free(partitions[0])
    return None


def _file_size_bytes(data, image_file_name):
    """Find a named file in q-filesystem data and return its size (KB -> bytes)."""
    if isinstance(data, dict):
        path = data.get("full-path") or data.get("name") or data.get("filename")
        if path and image_file_name in str(path):
            size = data.get("file-size") or data.get("size")
            try:
                return int(size) * 1024
            except (TypeError, ValueError):
                pass
        for value in data.values():
            found = _file_size_bytes(value, image_file_name)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _file_size_bytes(item, image_file_name)
            if found is not None:
                return found
    return None
