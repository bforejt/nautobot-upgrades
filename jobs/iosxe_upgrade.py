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
  1. Idempotency: if already on target, commit it if it is merely activated
     (cancelling a pending rollback), else no-op
  2. Pre-flight gates: version floor, install-mode (fail-closed), image
     resolution + compatibility, free-space
  3. Copy the image (device-initiated) and verify it arrived intact
  4. install add -> install activate (auto-rollback timer armed) -> reload
  5. Poll until the target version actually booted -> install commit
  6. Post-checks + sync Nautobot's Device.software_version
  7. Optional: install remove inactive (off by default)

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
from nautobot.dcim.models import (
    Device,
    DeviceType,
    Location,
    Platform,
    SoftwareImageFile,
    SoftwareVersion,
)
from nautobot.extras.choices import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)
from nautobot.extras.models import Role, SecretsGroup, SecretsGroupAssociation, Status, Tag

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

    # --- Optional filters: narrow the device picker for field operations ------
    location = MultiObjectVar(
        model=Location,
        required=False,
        description="Limit the device list to these locations.",
    )
    role = MultiObjectVar(
        model=Role,
        required=False,
        query_params={"content_types": "dcim.device"},
        description="Limit the device list to these device roles.",
    )
    status = MultiObjectVar(
        model=Status,
        required=False,
        query_params={"content_types": "dcim.device"},
        description="Limit the device list to these statuses.",
    )
    platform = MultiObjectVar(
        model=Platform,
        required=False,
        description="Limit the device list to these platforms.",
    )
    device_type = MultiObjectVar(
        model=DeviceType,
        required=False,
        description="Limit the device list to these device types.",
    )
    current_version = MultiObjectVar(
        model=SoftwareVersion,
        required=False,
        description="Limit the device list to devices currently on these versions.",
    )
    tags = MultiObjectVar(
        model=Tag,
        required=False,
        query_params={"content_types": "dcim.device"},
        description="Limit the device list to devices with these tags.",
    )

    devices = MultiObjectVar(
        model=Device,
        required=True,
        query_params={
            "location": "$location",
            "role": "$role",
            "status": "$status",
            "platform": "$platform",
            "device_type": "$device_type",
            "software_version": "$current_version",
            "tags": "$tags",
        },
        description=(
            "Target devices to upgrade. Use the filters above to narrow this list, "
            "then select the specific devices to act on (or select all)."
        ),
    )
    target_version = ObjectVar(
        model=SoftwareVersion,
        required=True,
        description=(
            "The Software Version (core dcim.SoftwareVersion) to upgrade to. Its "
            "Software Image File must have a download URL and, ideally, a file size."
        ),
    )
    secrets_group_override = ObjectVar(
        model=SecretsGroup,
        required=False,
        description=(
            "Optional override applied to ALL selected devices. By default each "
            "device uses its own assigned Secrets Group (Device > Secrets group); "
            "set this only to force a single group for this run."
        ),
    )
    assume_install_mode = BooleanVar(
        default=False,
        description=(
            "Proceed even if INSTALL vs BUNDLE boot mode cannot be confirmed over "
            "RESTCONF. Off (default) = fail closed; confirmed BUNDLE always aborts."
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
        field_order = [
            "location",
            "role",
            "status",
            "platform",
            "device_type",
            "current_version",
            "tags",
            "devices",
            "target_version",
            "secrets_group_override",
            "assume_install_mode",
            "remove_inactive",
            "debug",
            "dryrun",
        ]

    # ------------------------------------------------------------------ run --

    def run(
        self,
        *,
        location,
        role,
        status,
        platform,
        device_type,
        current_version,
        tags,
        devices,
        target_version,
        secrets_group_override,
        assume_install_mode,
        remove_inactive,
        debug,
        dryrun,
    ):
        # self.logger.success() exists only on Nautobot >= 2.4; fall back to info.
        log_success = getattr(self.logger, "success", self.logger.info)
        results = {}
        self.logger.info(
            "Starting IOS-XE upgrade to **%s** for %d selected device(s)%s.",
            target_version,
            len(devices),
            " (DRY-RUN)" if dryrun else "",
        )
        # The filters scope the device picker in the form; record any that were
        # applied for the audit trail.
        applied = {
            "location": location,
            "role": role,
            "status": status,
            "platform": platform,
            "device_type": device_type,
            "current_version": current_version,
            "tags": tags,
        }
        filter_summary = ", ".join(
            f"{key}={[str(v) for v in value]}" for key, value in applied.items() if value
        )
        if filter_summary:
            self.logger.info("Filters applied: %s.", filter_summary)
        for device in devices:
            try:
                summary = self._upgrade_device(
                    device,
                    target_version,
                    secrets_group_override,
                    remove_inactive,
                    debug,
                    dryrun,
                    assume_install_mode,
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
        self, device, target_version, override_group, remove_inactive, debug, dryrun,
        assume_install_mode,
    ):
        log = {"object": device}

        # -- 0. Credentials + reachability -----------------------------------
        host = self._device_host(device)
        username, password = self._credentials(device, override_group, log)
        client = RestconfClient(
            host, username, password, logger=self.logger, log_object=device, debug=debug
        )
        data = self._check_reachable(client, host)
        self.logger.info("RESTCONF reachable and authenticated at %s.", host, extra=log)

        # -- 1. Idempotency (commit-state aware) -----------------------------
        current = self._extract_version(data)
        self.logger.info("Current version: **%s**.", current or "unknown", extra=log)
        target_str = target_version.version
        if _version_tuple(current) and _version_tuple(current) == _version_tuple(target_str):
            return self._handle_already_on_target(client, device, target_version, dryrun, log)

        # -- 2. Pre-flight gates ---------------------------------------------
        self._gate_version_floor(current, log)
        self._gate_install_mode(client, log, assume_install_mode)

        image = self._resolve_image(device, target_version, log)
        self._gate_free_space(client, image, log)

        if dryrun:
            return (
                f"DRY-RUN ok: would copy '{image.download_url}' to "
                f"{C.TARGET_FS}{image.image_file_name} and install {target_str}. "
                "All pre-flight gates passed."
            )

        # -- 3. Transfer + integrity -----------------------------------------
        self._copy_image(client, image, log)
        self._verify_image(client, image, log)

        # -- 4. install add / activate / reload ------------------------------
        op_uuid = str(uuid_lib.uuid4())
        self._install_add(client, image, op_uuid, log)
        self._install_activate(client, image, op_uuid, log)

        # -- 5. Confirm booted, verify rollback net, commit, then sync -------
        self._wait_for_target(client, target_str, log)
        self._log_rollback_state(client, log)
        try:
            self._install_commit(client, op_uuid, log)
            self._verify_committed(client, target_str, log)
        except Exception as exc:  # noqa: BLE001 - real rollback risk if commit fails
            raise UpgradeAbort(
                f"Device booted {target_str} but COMMIT failed ({exc}). The device "
                "is ACTIVATED but NOT committed — re-run this job (it will commit) or "
                "roll back manually before the auto-abort timer expires."
            ) from exc
        # Commit succeeded: the device is safe even if the metadata update fails, so
        # a sync failure is logged AND surfaced in the result, but does NOT fail the
        # (committed) upgrade.
        sync_note = ""
        try:
            self._sync_nautobot(device, target_version, log)
        except Exception as exc:  # noqa: BLE001 - device committed; only Nautobot lagged
            self.logger.error(
                "Upgrade committed, but updating Nautobot Device.software_version "
                "failed (%s); update it manually.", exc, extra=log,
            )
            sync_note = " (Nautobot software_version update FAILED — set it manually)"

        # -- 6. Optional cleanup ---------------------------------------------
        if remove_inactive:
            self._remove_inactive(client, op_uuid, log)

        return f"Upgraded and committed to {target_str}.{sync_note}"

    def _handle_already_on_target(self, client, device, target_version, dryrun, log):
        """Device already runs the target version — but is it committed?

        Fail SAFE: treat it as a no-op only when we can positively confirm it is
        committed. Otherwise (activated/uncommitted, OR the state cannot be read or
        classified) run install commit anyway — committing an already-committed
        image is a harmless no-op, and it cancels a pending auto-rollback left by an
        interrupted prior run, which would otherwise silently revert the device.
        """
        target_str = target_version.version
        tokens = self._state_tokens(client, target_str)
        if _is_committed(tokens):
            return f"Already on target version {target_str} and committed; nothing to do."
        if dryrun:
            return (
                f"DRY-RUN: on target {target_str} but not confirmed committed "
                f"(state: {tokens or 'unknown'}); would run install commit to be safe."
            )
        self.logger.warning(
            "On target %s but not confirmed committed (state: %s); committing to be "
            "safe (cancels any pending auto-rollback).", target_str, tokens or "unknown",
            extra=log,
        )
        op_uuid = str(uuid_lib.uuid4())
        try:
            self._install_commit(client, op_uuid, log)
        except RestconfError as exc:
            # Committing when nothing is pending can error on some releases; the
            # device is already on the target version, so treat this as benign.
            self.logger.warning(
                "install commit on an already-on-target device returned an error "
                "(%s); it is likely already committed. Verify with 'show install "
                "summary'.", exc, extra=log,
            )
            return (
                f"On target {target_str}; commit returned an error (likely already "
                "committed — verify)."
            )
        self._verify_committed(client, target_str, log)
        try:
            self._sync_nautobot(device, target_version, log)
        except Exception as exc:  # noqa: BLE001 - committed; only Nautobot metadata lagged
            self.logger.error(
                "Committed, but updating Nautobot software_version failed (%s); "
                "update it manually.", exc, extra=log,
            )
        return f"On target {target_str}; ran install commit to ensure it is committed."

    # -------------------------------------------------------- helpers: setup --

    @staticmethod
    def _device_host(device):
        primary = device.primary_ip4 or device.primary_ip
        if not primary:
            raise UpgradeAbort("Device has no primary IP address.")
        return str(primary.host)

    def _credentials(self, device, override_group, log):
        # The device's own Secrets Group is the default; the job-level override
        # (if provided) takes precedence and applies to every selected device.
        group = override_group or device.secrets_group
        if not group:
            raise UpgradeAbort(
                "No Secrets Group assigned to the device and no override provided; "
                "cannot obtain RESTCONF credentials."
            )
        source = "job override" if override_group else "device"
        self.logger.info(
            "Using credentials from %s Secrets Group '%s'.", source, group, extra=log
        )
        username = self._secret(group, device, SecretsGroupSecretTypeChoices.TYPE_USERNAME)
        password = self._secret(group, device, SecretsGroupSecretTypeChoices.TYPE_PASSWORD)
        return username, password

    @staticmethod
    def _secret(group, device, secret_type):
        # Try the most specific access types first. A missing association for an
        # access type is expected (fall through); any OTHER error (provider down,
        # decryption/permission failure) is a real problem and aborts immediately
        # instead of being masked as "secret not found".
        candidates = [
            getattr(SecretsGroupAccessTypeChoices, attr, None)
            for attr in ("TYPE_RESTCONF", "TYPE_HTTP", "TYPE_REST", "TYPE_GENERIC")
        ]
        for access_type in [c for c in candidates if c]:
            try:
                return group.get_secret_value(
                    access_type=access_type, secret_type=secret_type, obj=device
                )
            except SecretsGroupAssociation.DoesNotExist:
                continue  # this access type isn't defined for the group
            except Exception as exc:  # noqa: BLE001 - real backend/decryption error
                raise UpgradeAbort(
                    f"Error retrieving the '{secret_type}' secret from Secrets Group "
                    f"'{group}' ({access_type}): {exc}"
                ) from exc
        raise UpgradeAbort(
            f"No '{secret_type}' secret defined in Secrets Group '{group}' for any "
            "of the RESTCONF/HTTP/Generic access types."
        )

    @staticmethod
    def _check_reachable(client, host):
        """Confirm RESTCONF is reachable AND the credentials authenticate.

        Returns the parsed device-system data (reused for the current-version read
        so we don't issue a second identical GET). Distinguishes authentication
        (401) and authorization/privilege (403) failures from plain connectivity.
        """
        try:
            return client.get(C.DATA_DEVICE_SYSTEM, ok_404=False) or {}
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

    @staticmethod
    def _extract_version(data):
        system = (data or {}).get("Cisco-IOS-XE-device-hardware-oper:device-system-data", {})
        return system.get("software-version")

    def _current_version(self, client):
        return self._extract_version(client.get(C.DATA_DEVICE_SYSTEM, ok_404=True) or {})

    def _state_tokens(self, client, version_str):
        """All install-oper state tokens for entries matching version_str."""
        data = client.get(C.DATA_INSTALL_OPER, ok_404=True) or {}
        return _version_state_tokens(data, version_str)

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

    def _gate_install_mode(self, client, log, assume_install_mode):
        data = client.get(C.DATA_INSTALL_OPER, ok_404=True)
        if not data:
            # install-oper entirely unreadable: every later gate (add/commit/
            # rollback confirmation) would also be blind, so refuse even WITH the
            # opt-in rather than run writes against an unobservable device.
            raise UpgradeAbort(
                "Could not read Cisco-IOS-XE-install-oper data; RESTCONF may lack "
                "the install model. The operational gates cannot function — refusing "
                "(assume_install_mode does not override an unreadable model)."
            )
        # Collect the boot mode of EVERY member. The enum is
        # install-mode-{install,bundle,install-bundle,...}; compare the suffix.
        suffixes = [
            str(m).lower().rsplit("install-mode-", 1)[-1]
            for m in _find_all_values(data, "install-mode")
        ]
        if any(s == "bundle" for s in suffixes):
            raise UpgradeAbort(
                "One or more members report BUNDLE mode, which this job does not "
                "support. Convert to INSTALL mode (boot flash:packages.conf) first."
            )
        if suffixes and all(s == "install" for s in suffixes):
            self.logger.info("Install-mode gate passed (install).", extra=log)
            return
        if suffixes and all(s in ("install", "install-bundle") for s in suffixes):
            # install-bundle = install mode booted from a .bin, not packages.conf —
            # an install variant (not plain bundle), so proceed but flag it.
            self.logger.warning(
                "Boot mode is install-bundle (%s) — install mode but booted from a "
                "bundle rather than packages.conf. Proceeding; verify intended.",
                suffixes, extra=log,
            )
            return
        if suffixes:
            # Present but unrecognized — fail closed regardless of the opt-in.
            raise UpgradeAbort(
                f"Unrecognized boot mode(s) {suffixes}; refusing (fail-closed). "
                "Verify the device is in install mode."
            )
        # Model readable but no boot-mode leaf found: only the opt-in proceeds.
        if assume_install_mode:
            self.logger.warning(
                "No boot-mode value found in install-oper; assume_install_mode is "
                "set, so proceeding. Verify with 'show version'.", extra=log,
            )
            return
        raise UpgradeAbort(
            "No boot-mode value found in install-oper. Set 'assume_install_mode' to "
            "proceed, or verify the device is in install mode."
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
    def _read_q_filesystem(client):
        """GET q-filesystem data, retrying briefly on transient RESTCONF errors.

        Returns None only if every attempt fails (so a one-off blip after a
        multi-minute copy doesn't get mistaken for 'no data').
        """
        for attempt in range(C.QFS_READ_RETRIES):
            try:
                return client.get(C.DATA_Q_FILESYSTEM, ok_404=True) or {}
            except RestconfError:
                if attempt + 1 >= C.QFS_READ_RETRIES:
                    return None
                time.sleep(C.POLL_INTERVAL)
        return None

    def _read_free_space(self, client):
        data = self._read_q_filesystem(client)
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

    def _read_file_size(self, client, image_file_name):
        data = self._read_q_filesystem(client)
        if not data:
            return None
        return _file_size_bytes(data, image_file_name)

    def _install_add(self, client, image, op_uuid, log):
        path = f"{C.TARGET_FS}{image.image_file_name}"
        version_str = image.software_version.version
        self.logger.info("install add %s ...", path, extra=log)
        payload = {"Cisco-IOS-XE-install-rpc:input": {"uuid": op_uuid, "path": path}}
        client.post_rpc(C.OP_INSTALL, payload, timeout=C.RPC_TIMEOUT)
        self._wait_for_added(client, version_str, log)

    def _wait_for_added(self, client, version_str, log):
        deadline = time.monotonic() + C.ADD_TIMEOUT
        while time.monotonic() < deadline:
            # Before 'install add' the target version is not in the install
            # inventory; once staged it appears (with some install state), so its
            # presence — scoped to the target version — confirms the add.
            if self._state_tokens(client, version_str):
                self.logger.info("install add staged (target version present).", extra=log)
                return
            time.sleep(C.POLL_INTERVAL)
        # Don't hard-fail on polling ambiguity — install-oper shape drifts between
        # releases. Warn and proceed; activate fails loudly if add never completed.
        self.logger.warning(
            "Could not confirm 'install add' for %s from install-oper within %ss; "
            "proceeding to activate.", version_str, C.ADD_TIMEOUT, extra=log,
        )

    def _install_activate(self, client, image, op_uuid, log):
        self.logger.info(
            "install activate → device reloads (auto-rollback timer: %s min)...",
            C.AUTO_ABORT_MINUTES, extra=log,
        )
        # 'activate' requires the mandatory choice (version/path/name); supply the
        # image path. Arm the auto-abort timer EXPLICITLY so the rollback window is
        # deterministic — if we never commit, the device reverts when it expires.
        # (auto-abort-timer-val is research-derived; verify the leaf per release.)
        payload = {
            "Cisco-IOS-XE-install-rpc:input": {
                "uuid": op_uuid,
                "path": f"{C.TARGET_FS}{image.image_file_name}",
                "auto-abort-timer-val": C.AUTO_ABORT_MINUTES,
            }
        }
        client.post_rpc(C.OP_ACTIVATE, payload, timeout=C.RPC_TIMEOUT, tolerate_disconnect=True)

    def _wait_for_target(self, client, target_str, log):
        """Wait for the device to reload AND stably report the target version.

        Polls the booted version within RELOAD_TIMEOUT. Transient connection errors
        and not-yet-populated reads are treated as 'still coming up' (no false
        'wrong version' abort). Requires the target to be seen on TWO consecutive
        polls before returning, so a single early/transient read from a
        partially-converged control plane does not trigger the irreversible commit.
        """
        self.logger.info("Waiting for reload and the target version to come up...", extra=log)
        time.sleep(C.RELOAD_INITIAL_SLEEP)
        target = _version_tuple(target_str)
        deadline = time.monotonic() + C.RELOAD_TIMEOUT
        went_down = False
        online = False
        last_seen = None
        consecutive = 0
        while time.monotonic() < deadline:
            try:
                booted = self._current_version(client)
            except RestconfError:
                booted = None
            if booted is None:
                went_down = True  # observed the reboot (unreachable at least once)
            elif not online:
                online = True
                self.logger.info("Device is back online.", extra=log)
            # Only accept the target AFTER we've seen the device go down, so a box
            # that never actually reloaded cannot satisfy the confirmation.
            if went_down and _version_tuple(booted) == target:
                consecutive += 1
                if consecutive >= 2:
                    self.logger.info(
                        "Confirmed booted target version **%s** (stable).", booted, extra=log
                    )
                    return
            else:
                consecutive = 0
                if _version_tuple(booted):
                    last_seen = booted
            time.sleep(C.POLL_INTERVAL)
        raise UpgradeAbort(
            f"Device did not stably boot the target version within {C.RELOAD_TIMEOUT}s "
            f"(went_down={went_down}, online={online}, last definite version: "
            f"{last_seen or 'unknown'}). NOT committing — the auto-rollback timer "
            "should revert it; verify and roll back manually if needed."
        )

    def _log_rollback_state(self, client, log):
        """Best-effort: confirm an auto-abort (rollback) timer appears armed.

        We armed it on activate, but the leaf is release-dependent, so verify it is
        actually pending before relying on it. If it cannot be confirmed, warn
        loudly rather than letting later messaging promise protection we don't have.
        """
        data = client.get(C.DATA_INSTALL_OPER, ok_404=True) or {}
        timer = None
        for key in ("auto-abort-timer", "auto-abort-timer-val", "abort-timer", "remaining-time"):
            for value in _find_all_values(data, key):
                text = str(value).strip().lower()
                if text and text not in ("0", "false", "no", "none", "disabled"):
                    timer = value
                    break
            if timer is not None:
                break
        if timer is not None:
            self.logger.info("Auto-rollback timer appears armed (%s).", timer, extra=log)
        else:
            self.logger.warning(
                "Could not confirm an auto-rollback timer is armed; if commit is "
                "interrupted the device may NOT auto-revert — be ready to roll back "
                "manually.", extra=log,
            )

    def _install_commit(self, client, op_uuid, log):
        self.logger.info("install commit (making the new image permanent)...", extra=log)
        payload = {"Cisco-IOS-XE-install-rpc:input": {"uuid": op_uuid}}
        client.post_rpc(C.OP_COMMIT, payload, timeout=C.RPC_TIMEOUT)

    def _verify_committed(self, client, version_str, log):
        tokens = self._state_tokens(client, version_str)
        if _is_committed(tokens):
            self.logger.info("Commit confirmed via install-oper (state: %s).", tokens, extra=log)
        else:
            self.logger.warning(
                "Could not confirm committed state for %s from install-oper (state: "
                "%s); verify with 'show install summary'.", version_str,
                tokens or "unknown", extra=log,
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


def _find_all_values(data, key):
    """Return every scalar value stored under ``key`` anywhere in ``data``."""
    out = []
    if isinstance(data, dict):
        for found_key, value in data.items():
            if found_key == key and isinstance(value, (str, int, float)):
                out.append(value)
            out.extend(_find_all_values(value, key))
    elif isinstance(data, list):
        for item in data:
            out.extend(_find_all_values(item, key))
    return out


def _version_state_tokens(data, version_str):
    """All install-oper state tokens (lowercased) for entries matching version_str.

    Collects EVERY state of the target version (per-member rows, and historical
    rows for the same version), not just the first match, so a stale lower-priority
    entry cannot mask the live state. Callers reduce these with _is_committed().
    """
    target = _version_tuple(version_str)
    if target is None:
        return []
    tokens = []
    _collect_states(data, target, tokens)
    return tokens


def _collect_states(node, target, out):
    if isinstance(node, dict):
        has_version = any(
            "version" in key.lower()
            and isinstance(value, (str, int, float))
            and _version_tuple(value) == target
            for key, value in node.items()
        )
        if has_version:
            for key, value in node.items():
                # Collect any non-empty string under a state-named key; the VALUE
                # may be a full enum ('install-state-committed') OR a short code
                # ('C'/'A'/'U'/'I') OR a plain word — do not require it to contain
                # the literal 'state'. _classify_state() normalizes all of these.
                if "state" in key.lower() and isinstance(value, str) and value.strip():
                    out.append(value.strip().lower())
        for value in node.values():
            _collect_states(value, target, out)
    elif isinstance(node, list):
        for item in node:
            _collect_states(item, target, out)


def _classify_state(token):
    """Normalize an install-oper state value to a canonical state.

    Handles full enums ('install-state-committed'), short codes ('C'/'A'/'U'/'I'),
    and plain words ('committed'/'activated'/'inactive'). Ordered so 'uncommitted'
    is never misread as 'committed' and 'inactive' is never misread as 'activated'.
    """
    t = token.strip().lower().rsplit("state-", 1)[-1]
    if t in ("c", "committed"):
        return "committed"
    if t in ("u", "uncommitted"):
        return "uncommitted"
    if t in ("a", "activated", "active"):
        return "activated"
    if t in ("i", "inactive", "added"):
        return "added"
    if "uncommitted" in t:
        return "uncommitted"
    if "committed" in t:
        return "committed"
    if "inactive" in t or "added" in t:
        return "added"
    if "activ" in t:
        return "activated"
    return "other"


def _is_committed(tokens):
    """True only if a committed state is present and no pending state is.

    'Pending' = activated or uncommitted. Aggregating across rows is conservative:
    a stale pending row makes this return False, so the caller commits to be safe
    (a harmless no-op if already committed) rather than risk skipping a real commit.
    """
    classes = [_classify_state(t) for t in tokens]
    return "committed" in classes and not any(c in ("activated", "uncommitted") for c in classes)


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
    """Free bytes on the target filesystem from q-filesystem data (KB -> bytes).

    Matches a partition whose name equals a configured name OR is that name with a
    stack-member suffix ('flash-1', 'flash:1') — but never 'bootflash'/'usbflash',
    and never an unrelated single partition. Returns None when the target
    filesystem can't be found, which makes the caller abort rather than validate
    space on the wrong filesystem.
    """
    for partition in _find_partitions(data):
        name = str(partition.get("name", "")).strip().rstrip(":").lower()
        for fs in fs_names:
            if name == fs or name.startswith(fs + "-") or name.startswith(fs + ":"):
                free = _partition_free(partition)
                if free is not None:
                    return free
    return None


def _file_size_bytes(data, image_file_name):
    """Find a named file in q-filesystem data and return its size (KB -> bytes).

    Matches the file's basename for EQUALITY (not substring) so a longer filename
    that merely contains the target name can't mask a truncated copy.
    """
    if isinstance(data, dict):
        path = data.get("full-path") or data.get("name") or data.get("filename")
        if path:
            basename = str(path).split(":")[-1].rsplit("/", 1)[-1]
            if basename == image_file_name:
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
