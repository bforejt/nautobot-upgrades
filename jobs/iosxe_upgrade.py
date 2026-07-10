"""Cisco IOS-XE software upgrade Job (Catalyst 9300 family, C8000V) — RESTCONF only.

This Job upgrades one or more Cisco IOS-XE devices to a target software version
using INSTALL mode, driven entirely over RESTCONF. It behaves like a cautious
engineer: every step is a PASS/FAIL gate, and the job stops on the first failed
gate for a device rather than pushing forward.

Scope (kept deliberately small):
  * IOS-XE devices running >= 17.9.1: the Catalyst 9300 family (9300 /
    9300L / 9300LM / 9300X — one shared cat9k image and install flow) and the
    Catalyst 8000V (autonomous mode; its bootflash: filesystem is discovered
    from the device automatically). 17.9.1 is the lowest
    release where every model the job relies on is complete (operation ledger
    17.8.1+, sys-activity/boot-mode 17.5.1+, byte-exact file sizes 17.9.1+).
    17.5-17.8 are refused (their file sizes are kilobyte-described, which
    would false-abort the copy verification); below 17.5.1 the models are
    missing outright. Hardware-validated baseline: 17.15.x.
  * Reads target version + image metadata from CORE Nautobot
    (dcim.SoftwareVersion / dcim.SoftwareImageFile). No Device Lifecycle app
    dependency.
  * Credentials come from the device's core Secrets Group (or an override).

Upgrade flow (per device):
  0. Resolve credentials + RESTCONF reachability
  1. Idempotency: if already on target, commit it if it is merely activated
     (cancelling a pending rollback), else no-op
  2. Pre-flight gates: version floor, install-mode (fail-closed), image
     resolution + compatibility; optional operator-requested CLEAN (remove
     inactive/staged software — the deliberate staged-conflict override);
     target-filesystem discovery from the device; free-space (minimum across
     stack members, evaluated on the cleaned flash)
  3. Classic copy RPC in a worker thread, with the on-device file size polled
     for progress reporting and a size verification on completion
     (Run scope 'stage-copy' STOPS here — staged, nothing armed)
  4. install add -> tracked to COMPLETION in the engine's operation ledger
     (install-oper / install-oper-hist records keyed by our RPC uuid; install
     state inference only as a fallback; Run scope 'stage-add' STOPS here) ->
     engine-idle gate (sys-activity) -> install activate (non-ISSU, by full
     internal version; re-sent on ledger-absent evidence; ledger-ENGAGED runs
     get an extended budget for microcode reprogramming; rollback timer
     checked after reload) -> reload
  5. Poll until the target version actually booted AND every pre-upgrade
     stack member rejoined -> install commit (ledger-tracked)
  6. Post-checks + sync Nautobot's Device.software_version
  7. Optional: install remove inactive (off by default)

NOTE: The core flow is hardware-validated (Catalyst 9300 single switches and
a 2-member stack; trains 17.12 -> 17.15 <-> 17.18 <-> 26.1; lettered rebuilds;
serial batches; from Nautobot 3.1 and 2.4). The project remains under active
development - new capabilities carry their validation state in the README -
and every run should start with Dry-run.
"""

from __future__ import annotations

import math
import re
import threading
import time
import urllib.parse as urllib_parse
import uuid as uuid_lib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from celery.exceptions import SoftTimeLimitExceeded

try:  # Celery task-context propagation into worker threads (see run()).
    from celery import current_task
    from celery.app import pop_current_task, push_current_task
except ImportError:  # pragma: no cover - non-Celery environments (tests)
    current_task = None
    pop_current_task = push_current_task = None
from django.db import close_old_connections, transaction
from nautobot.apps.jobs import (
    BooleanVar,
    ChoiceVar,
    DryRunVar,
    IntegerVar,
    Job,
    MultiObjectVar,
    ObjectVar,
)
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

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)([a-z])?", re.IGNORECASE)


class UpgradeAbort(Exception):
    """A safety gate failed; abort this device's upgrade (not the whole job)."""


class LedgerOpFailure(UpgradeAbort):
    """The device's operation ledger RECORDED a failure for our operation.

    Distinct from a refusal/transport error so callers that treat commit errors
    as benign (already-on-target) can still surface a real recorded failure.
    """


def _fmt_duration(seconds):
    """Human duration for planning logs: '47s', '14m32s', '1h02m'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _version_key(text):
    """((major, minor, patch), rebuild-letter) from any IOS-XE version string.

    Handles '17.3.1', Cisco's zero-padded '17.09.04', REBUILD letters
    ('17.15.4d' / device-form '17.15.04D' -> ((17,15,4), 'd')), full banner
    strings, image filenames, and internal identifiers ('17.15.04d.0.6839').
    Rebuild letters are DISTINCT versions: 17.15.4d != 17.15.4 — the letter is
    part of the identity, so base->rebuild (and back) are real upgrades.
    """
    match = _VERSION_RE.search(str(text or ""))
    if not match:
        return None
    numbers = tuple(int(part) for part in match.groups()[:3])
    return (numbers, (match.group(4) or "").lower())


def _version_tuple(text):
    """Numeric (major, minor, patch) only — for ORDERING (the version floor).

    Rebuild letters never affect ordering against the floor; equality checks
    must use _version_key so letters count.
    """
    key = _version_key(text)
    return key[0] if key else None


class IOSXEUpgrade(Job):
    """Upgrade Cisco IOS-XE devices (9300 family, C8000V) over RESTCONF install mode."""

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
    run_scope = ChoiceVar(
        choices=(
            # Listed in pipeline order; the SAFE step is the default so a
            # forgotten dropdown can never reload a device (a real upgrade
            # requires deliberately selecting Full).
            ("stage-copy", "Step 1 - Copy image (default)"),
            ("stage-add", "Steps 1 & 2 - Copy image and prep"),
            ("full", "Full - Copy, Activate, Reload"),
        ),
        default="stage-copy",
        required=False,
        description=(
            "Order of operations: copy the image to the device (Step 1) → prep "
            "it for activation (Step 2, 'install add' — extracted, distributed "
            "to all members, marked for activation; no reload, nothing armed) → "
            "activate + reload + commit (Full — THE ONLY CHOICE THAT RELOADS). "
            "The safe copy-only step is the default; each later run skips work "
            "already done, so staging ahead collapses the maintenance window to "
            "roughly the reload. Staging causes no outage and is safe at high "
            "Parallelism during business hours."
        ),
    )
    clean_before = BooleanVar(
        label="Clean device first (removes inactive & staged images!)",
        default=False,
        description=(
            "⚠️ BE CAREFUL. Before upgrading, remove ALL software this device "
            "is not running: inactive packages, leftover image files, AND any "
            "version another engineer may have staged — this deliberately "
            "overrides the staged-conflict safety stop, which exists because a "
            "staged version usually means a change is already in flight. It "
            "also deletes the previous version kept for soak-period rollback "
            "(rolling back afterward = re-run this job targeting the old "
            "version). Tick only when you know the state of the network and "
            "that nothing else is planned for this device. Independent of "
            "'Remove inactive (after commit)'."
        ),
    )
    save_config = BooleanVar(
        label="Save running-config before reload (Full runs)",
        default=False,
        description=(
            "Before the activation reload, write running-config to "
            "startup-config ('write memory' over RESTCONF). RPC-triggered "
            "reloads never ask the CLI's 'System configuration has been "
            "modified. Save?' question — unsaved changes are silently lost. "
            "Full runs always CHECK the device's config timestamps and warn "
            "if running-config looks unsaved; this box makes the job save it. "
            "Off by default: saving is itself a write, and it would persist "
            "half-applied changes an engineer deliberately left unsaved."
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
    remove_inactive = BooleanVar(
        label="Remove inactive (after commit)",
        default=False,
        description=(
            "AFTER this run's successful commit, run 'install remove inactive' "
            "to reclaim space. Off by default so the previous image is kept for "
            "a soak period. This does NOT clear previously staged images before "
            "an upgrade — a different staged version aborts the run with a "
            "warning instead, deliberately: it usually means another change is "
            "already in flight on that device."
        ),
    )
    parallelism = IntegerVar(
        default=C.DEFAULT_PARALLELISM,
        min_value=1,
        max_value=C.MAX_PARALLELISM,
        description=(
            "Devices upgraded concurrently (1 = one at a time). Each device is "
            "fully independent; per-device logs interleave in time order but stay "
            "attributed to their device. Size to your firmware server's capacity "
            "for simultaneous image pulls."
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
            "9300-family switches and Catalyst 8000V routers, entirely over "
            "RESTCONF. Requires IOS-XE >= 17.9.1 with RESTCONF enabled."
        )
        has_sensitive_variables = False
        dryrun_default = True
        # With parallel batches the makespan is ~ceil(devices / parallelism) x
        # one worst-case device (copy 3600 + add 1200 + reload 120+1800 + slack);
        # size batches so that fits inside the soft limit. SoftTimeLimitExceeded
        # is re-raised (never swallowed); queued devices are cancelled and named,
        # in-flight devices are recovered by an idempotent re-run.
        soft_time_limit = 7200
        time_limit = 8400
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
            "run_scope",
            "clean_before",
            "save_config",
            "secrets_group_override",
            "remove_inactive",
            "parallelism",
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
        run_scope,
        clean_before,
        save_config,
        secrets_group_override,
        remove_inactive,
        parallelism,
        debug,
        dryrun,
    ):
        # self.logger.success() exists only on Nautobot >= 2.4; fall back to info.
        log_success = getattr(self.logger, "success", self.logger.info)
        results = {}
        failed = []
        # Cooperative-stop signal for the time-budget path: worker threads
        # check it at every polling loop and halt at a safe boundary.
        self._stop = threading.Event()
        # Nautobot's job-log handler binds records to the JobResult through
        # TWO separate Celery thread-locals (verified against Nautobot's
        # add_nautobot_log_handler + celery's TaskFormatter):
        #   1. current_task — the handler's `if current_task is None: return`;
        #   2. task.request (the REQUEST stack) — TaskFormatter stamps
        #      record.task_id from task.request.id; a worker thread sees a
        #      blank Context (id=None), the JobResult lookup fails, and the
        #      record is silently dropped even with the task pushed.
        # Capture BOTH in the main thread so each worker can push them onto
        # its own thread-local stacks.
        celery_task = None
        celery_request = None
        if current_task is not None:
            try:
                celery_task = current_task._get_current_object()
                if celery_task is not None:
                    request = celery_task.request
                    # Only a real in-worker request carries the task id; a
                    # blank default Context would reintroduce the silent drop.
                    celery_request = request if getattr(request, "id", None) else None
            except Exception:  # noqa: BLE001 - no task context (tests, shell)
                celery_task = None
                celery_request = None
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
        device_list = list(devices)

        def _one_device(device):
            """Full per-device upgrade in a worker thread.

            Returns (device, summary, failed_bool); never raises for per-device
            problems (batch isolation). Django opens ORM connections per thread
            (the job logger and the Nautobot sync both hit the DB), so stale
            connections are closed on entry and exit.
            """
            # Bind this thread to the Celery task AND its request context so
            # Nautobot's DB log handler can resolve the JobResult — without
            # both, every log line from a worker thread is silently dropped.
            if celery_task is not None and push_current_task is not None:
                push_current_task(celery_task)
                if celery_request is not None:
                    celery_task.request_stack.push(celery_request)
            close_old_connections()
            device_started = time.monotonic()
            try:
                summary = self._upgrade_device(
                    device,
                    target_version,
                    secrets_group_override,
                    remove_inactive,
                    debug,
                    dryrun,
                    run_scope,
                    clean_before,
                    save_config,
                )
                # Total wall-clock per device — the number change windows are
                # planned around.
                summary = f"{summary} [total: {_fmt_duration(time.monotonic() - device_started)}]"
                return device, summary, False
            except UpgradeAbort as exc:
                elapsed = _fmt_duration(time.monotonic() - device_started)
                self.logger.error(
                    "Upgrade aborted after %s: %s", elapsed, exc, extra={"object": device}
                )
                return device, f"ABORTED after {elapsed}: {exc}", True
            except RestconfError as exc:
                hint = _auth_hint(exc.status_code)
                elapsed = _fmt_duration(time.monotonic() - device_started)
                self.logger.error(
                    "RESTCONF error after %s: %s%s", elapsed, exc, hint, extra={"object": device}
                )
                return device, f"RESTCONF error after {elapsed}: {exc}{hint}", True
            except Exception as exc:  # noqa: BLE001 - surface anything unexpected
                elapsed = _fmt_duration(time.monotonic() - device_started)
                self.logger.error(
                    "Unexpected error after %s: %s", elapsed, exc, extra={"object": device}
                )
                return device, f"UNEXPECTED error after {elapsed}: {exc}", True
            finally:
                close_old_connections()
                if celery_task is not None and pop_current_task is not None:
                    if celery_request is not None:
                        celery_task.request_stack.pop()
                    pop_current_task()

        # Each device is fully independent (own RESTCONF sessions, own operation
        # uuids, own gates), so a batch runs up to 'parallelism' devices
        # concurrently. Threads suit the workload: it is almost entirely waiting
        # on the network (copy, install, reload).
        workers = max(1, min(int(parallelism or 1), C.MAX_PARALLELISM, len(device_list) or 1))
        if workers > 1:
            self.logger.info(
                "Running up to %d device upgrade(s) in parallel (per-device logs "
                "interleave in time order; each entry stays attributed to its "
                "device).",
                workers,
            )
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="iosxe-upgrade")
        futures = {executor.submit(_one_device, device): device for device in device_list}
        try:
            for future in as_completed(futures):
                device, summary, device_failed = future.result()
                results[device.name] = summary
                if device_failed:
                    failed.append(device.name)
                else:
                    log_success(summary, extra={"object": device})
            executor.shutdown(wait=True)
        except SoftTimeLimitExceeded:
            # Never swallow the time budget — but NEVER leave worker threads
            # running behind a finished task either: Celery raises this in the
            # MAIN thread only, and completing the task CANCELS the hard limit,
            # so an immediate re-raise would orphan live threads still driving
            # switches (and invite a re-run to race them). Instead: signal the
            # cooperative stop, wait for in-flight devices to halt at their
            # next safe checkpoint (bounded by one poll interval + one RPC —
            # well inside the soft->hard grace), then account for EVERY device.
            self._stop.set()
            self.logger.error(
                "Stop signal received (soft time limit, or an operator ran "
                "'Cancel IOS-XE Upgrade Run') — stopping in-flight device "
                "upgrades at their next safe checkpoint..."
            )
            executor.shutdown(wait=True, cancel_futures=True)
            never_started = []
            for future, device in futures.items():
                if future.cancelled():
                    never_started.append(device.name)
                elif future.done() and device.name not in results:
                    # Completed while the signal was in flight — never drop a
                    # finished device's outcome.
                    _, summary, device_failed = future.result()
                    results[device.name] = summary
                    if device_failed:
                        failed.append(device.name)
            self.logger.error(
                "Time-budget post-mortem — completed: %s; failed or stopped "
                "mid-flight (each entry above has its reason; stopped devices "
                "are at safe boundaries and safe to re-run): %s; never "
                "started: %s.",
                ", ".join(sorted(n for n in results if n not in failed)) or "none",
                ", ".join(sorted(failed)) or "none",
                ", ".join(sorted(never_started)) or "none",
            )
            raise
        if failed:
            # Per-device isolation is deliberate (one bad device must not stop
            # the batch), but the JOB must not report green when any device
            # failed — Nautobot marks a job FAILED only when run() raises, so
            # raise after the batch completes. The full per-device breakdown is
            # logged above (a raised failure replaces the return value).
            succeeded = [name for name in results if name not in failed]
            self.logger.error(
                "Run finished: %d succeeded (%s), %d FAILED (%s).",
                len(succeeded),
                ", ".join(sorted(succeeded)) or "none",
                len(failed),
                ", ".join(sorted(failed)),
            )
            raise RuntimeError(
                f"{len(failed)} of {len(device_list)} device(s) failed: "
                f"{', '.join(sorted(failed))} — see the per-device errors in the "
                "job log. Devices that succeeded are committed and unaffected."
            )
        return results

    # ----------------------------------------------------------- orchestrate --

    def _upgrade_device(
        self,
        device,
        target_version,
        override_group,
        remove_inactive,
        debug,
        dryrun,
        run_scope="full",
        clean_before=False,
        save_config=False,
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
        # _version_key: rebuild letters count (17.15.4d is NOT 17.15.4), so a
        # base->rebuild upgrade (or a rebuild rollback) proceeds as a real run.
        if _version_key(current) and _version_key(current) == _version_key(target_str):
            return self._handle_already_on_target(client, device, target_version, dryrun, log)

        # -- 2. Pre-flight gates ---------------------------------------------
        self._gate_version_floor(current, log)
        self._gate_install_mode(client, log)

        image = self._resolve_image(device, target_version, log)
        # Operator-requested pre-upgrade clean (deliberate override of the
        # staged-conflict stop) — never in dry-run (it writes). Runs BEFORE the
        # free-space gate so the gate evaluates the CLEANED flash.
        if clean_before and not dryrun:
            self._clean_device(client, target_str, log)

        # Discover the writable filesystem from the device itself (flash: on
        # Catalyst switches, bootflash: on C8000V) — every downstream step
        # (space gate, copy destination, install add path) uses this value.
        # Per-device local, never instance state: device threads run in parallel.
        target_fs = self._discover_target_fs(client, log)
        self._gate_free_space(client, image, log, target_fs)

        # Catalyst 9800 WLC guidance (warn, never gate — the operator owns the
        # choice): this job upgrades the CONTROLLER only. It does not perform
        # AP image predownload, so a full-scope reload forces every joined AP
        # to download its image afterward — an extended wireless outage.
        # Detection is by the image being installed (all 9800 images are
        # named C9800-*): the strongest signal, available even in dry-run.
        if "c9800" in str(image.image_file_name).lower():
            if run_scope == "full":
                self.logger.warning(
                    "Catalyst 9800 WLC image detected. This job upgrades the "
                    "CONTROLLER ONLY — it does NOT predownload AP images. After "
                    "the reload, every joined AP must download the new image "
                    "before rejoining (CAPWAP requires matching versions): "
                    "expect an EXTENDED wireless outage — minutes to hours at "
                    "fleet scale. Proceed only if a full wireless outage is "
                    "acceptable (lab, or a full-outage window). On HA SSO pairs "
                    "BOTH controllers reload together. A wireless-aware mode "
                    "(AP predownload orchestration) is planned but not built.",
                    extra=log,
                )
            else:
                self.logger.info(
                    "Catalyst 9800 WLC image detected. Staging (copy/add) is "
                    "safe on a 9800 — nothing reloads. Note for the eventual "
                    "activation: this job does not predownload AP images, so a "
                    "full-scope run causes an extended wireless outage until a "
                    "wireless-aware mode is built.",
                    extra=log,
                )

        # Advisory (info, not warning — leftover images are normal during soak
        # periods): if a DIFFERENT version is staged/added, say so before we
        # spend ~15 minutes on a copy the install engine may refuse.
        entries, staged = self._inventory_other_versions(client, target_str)
        if staged:
            self.logger.info(
                "Install DB also tracks other version(s): %s. A staged version "
                "(%s) usually means someone ALREADY prepared an upgrade on this "
                "device — if this run targets something else, check for a "
                "change in flight before proceeding. The install engine may "
                "refuse this run; clearing staged code is a deliberate act: "
                "if you OWN this device's change, re-run with 'Clean device "
                "first' ticked (or CLI 'install remove inactive'). The "
                "Remove-inactive option runs only AFTER a successful commit "
                "and does not do this.",
                "; ".join(entries),
                " / ".join(staged),
                extra=log,
            )

        if dryrun:
            if run_scope == "stage-copy":
                planned = (
                    f"would PRE-STAGE (copy only): '{image.download_url}' to "
                    f"{target_fs}{image.image_file_name} — no install activity"
                )
            elif run_scope == "stage-add":
                planned = (
                    f"would PRE-STAGE (copy + install add) {target_str} — no activate, no reload"
                )
            else:
                planned = (
                    f"would copy '{image.download_url}' to "
                    f"{target_fs}{image.image_file_name} and install {target_str}"
                )
            clean_note = ""
            if clean_before:
                pre_entries, _ = self._inventory_other_versions(client, target_str)
                clean_note = (
                    " Would FIRST clean the device (remove inactive/staged "
                    f"software; install DB currently also tracks: "
                    f"{'; '.join(pre_entries) or 'nothing'})."
                )
            cfg_note = ""
            if run_scope == "full":
                # Read-only check, surfaced here because only Full reloads —
                # and the RPC reload never prompts to save.
                state, detail, _ticks = self._config_sync_state(client)
                if state is False:
                    action = (
                        "would save before the reload"
                        if save_config
                        else "the reload would NOT save them (tick 'Save "
                        "running-config before reload' or save manually)"
                    )
                    cfg_note = f" Running-config has UNSAVED changes ({detail}) — {action}."
                elif state is None:
                    cfg_note = f" Config-sync state could not be determined ({detail})" + (
                        "; would save before the reload regardless."
                        if save_config
                        else "; save manually if in doubt."
                    )
                elif save_config:
                    cfg_note = (
                        " startup-config is already current; would save again anyway (harmless)."
                    )
            return f"DRY-RUN ok:{clean_note}{cfg_note} {planned}. All pre-flight gates passed."

        # -- 3. Transfer + integrity (classic copy in a worker thread, watched
        # to a size-verified completion inside _copy_image) --------------------
        self._copy_image(client, image, log, target_fs)

        if run_scope == "stage-copy":
            # Pre-staging stops HERE, structurally before any code path that
            # can reach activate (the only disruptive verb). Nothing is armed;
            # nothing reloads; a re-run skips the verified copy.
            return (
                f"STAGED (copy): '{image.image_file_name}' is on {target_fs} "
                "and size-verified. Run again with scope 'full' during the "
                "maintenance window — the copy will be skipped."
            )

        # -- 4. install add / activate (verified started) / reload -----------
        # Capture the stack member roster first: after the reload, every member
        # must rejoin before we commit.
        roster = self._member_roster(client)
        if roster:
            self.logger.info(
                "Stack roster captured: %d member(s) (%s).",
                len(roster),
                sorted(roster),
                extra=log,
            )
        # Each write gets its OWN correlation uuid: the engine's operation
        # ledger is keyed by it, so per-operation uuids keep the tracking exact
        # (one shared uuid would make add records vouch for the commit).
        ledger_confirmed_add = self._install_add(
            client, image, str(uuid_lib.uuid4()), log, target_fs
        )
        if run_scope == "stage-add":
            # Pre-staging stops HERE — the image is extracted, distributed to
            # every member, and marked for activation in the install DB (a
            # supported resting state that survives power cycles; no rollback
            # timer armed, boot variable untouched). The window run needs only
            # activate -> reload -> commit.
            return (
                f"STAGED (add): {target_str} is marked for activation. The "
                "maintenance-window run (scope 'full') will skip the copy and "
                "the add, and needs only activate → reload → commit."
            )
        # -- Config-sync before the reload: the RPC-triggered activation
        # reload never asks the CLI's 'configuration modified. Save?' question
        # — unsaved running-config changes would be silently lost. Decision
        # from device-published timestamps; warn-don't-gate (the operator owns
        # the choice) unless the save was explicitly requested. Runs BEFORE
        # the engine-idle gate so the idle-confirmation-to-activate gap stays
        # minimal.
        state, detail, ticks = self._config_sync_state(client)
        if save_config:
            self._save_config(client, log, pre_ticks=ticks)
        elif state is False:
            self.logger.warning(
                "Running-config has changes newer than startup-config (%s). "
                "The activation reload will NOT save them — save manually now "
                "('write memory') or re-run with 'Save running-config before "
                "reload' ticked. (A device that has not saved since boot also "
                "reads this way.)",
                detail,
                extra=log,
            )
        elif state is None:
            self.logger.warning(
                "Could not determine whether running-config is saved (%s) — "
                "save manually if in doubt; the activation reload will not.",
                detail,
                extra=log,
            )
        else:
            self.logger.info(
                "startup-config is current (%s) — nothing to lose in the reload.",
                detail,
                extra=log,
            )
        self._wait_for_engine_idle(
            client, log, "install activate", settle_fallback=not ledger_confirmed_add
        )
        act_uuid = str(uuid_lib.uuid4())
        resend = self._install_activate(client, image, act_uuid, log)
        self._confirm_activation(client, target_str, act_uuid, log, resend)

        # -- 5. Confirm booted + all members back, rollback net, commit, sync --
        self._wait_for_target(client, target_str, log)
        self._verify_members(client, roster, log)
        self._log_rollback_state(client, log)
        try:
            committed = self._install_commit(client, target_str, log)
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
                "failed (%s); update it manually.",
                exc,
                extra=log,
            )
            sync_note = " (Nautobot software_version update FAILED — set it manually)"

        # -- 6. Optional cleanup ---------------------------------------------
        if remove_inactive:
            if committed:
                self._remove_inactive(client, log)
            else:
                self.logger.warning(
                    "Skipping 'install remove inactive': commit not yet confirmed.",
                    extra=log,
                )
        elif committed:
            self.logger.info(
                "Previous version's files were left on flash (may show as untracked "
                "leftovers rather than in 'show install inactive'); re-run with "
                "'Remove inactive' later to reclaim space. For a guaranteed "
                "rollback path during soak, keep the previous version's image "
                "registered in Nautobot and hosted on the firmware server — "
                "downgrading is then just a run of this job with that version as "
                "the target.",
                extra=log,
            )

        if not committed:
            return (
                f"Upgraded to {target_str}; commit issued but not yet confirmed — "
                f"verify with 'show install summary'.{sync_note}"
            )
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
            "safe (cancels any pending auto-rollback).",
            target_str,
            tokens or "unknown",
            extra=log,
        )
        try:
            self._install_commit(client, target_str, log)
        except LedgerOpFailure as exc:
            # The engine RECORDED a commit failure — device-published state, not
            # a benign "nothing to commit" refusal. The image is uncommitted and
            # an auto-rollback timer may be ticking: surface it loudly.
            raise UpgradeAbort(
                f"Device is on {target_str} but NOT committed, and the install "
                f"engine recorded a commit FAILURE: {exc} — intervene before any "
                "auto-rollback timer expires ('show install summary', 'install "
                "commit' from the CLI)."
            ) from exc
        except (RestconfError, UpgradeAbort) as exc:
            # Committing when nothing is pending can error on some releases (an
            # HTTP error or a refusal body); the device is already on the target
            # version, so treat this as benign.
            self.logger.warning(
                "install commit on an already-on-target device returned an error "
                "(%s); it is likely already committed. Verify with 'show install "
                "summary'.",
                exc,
                extra=log,
            )
            return (
                f"On target {target_str}; commit returned an error (likely already "
                "committed — verify)."
            )
        try:
            self._sync_nautobot(device, target_version, log)
        except Exception as exc:  # noqa: BLE001 - committed; only Nautobot metadata lagged
            self.logger.error(
                "Committed, but updating Nautobot software_version failed (%s); "
                "update it manually.",
                exc,
                extra=log,
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
        self.logger.info("Using credentials from %s Secrets Group '%s'.", source, group, extra=log)
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
            raise UpgradeAbort(f"Could not determine the running IOS-XE version (got {current!r}).")
        if current_tuple < C.MIN_IOSXE_VERSION:
            raise UpgradeAbort(
                f"Running version {current} is below {floor}. RESTCONF-driven "
                "install is unavailable on this release; upgrade out of band first."
            )
        self.logger.info("Version floor gate passed (>= %s).", floor, extra=log)

    def _gate_install_mode(self, client, log):
        data = client.get(C.DATA_INSTALL_OPER, ok_404=True)
        if not data:
            # install-oper entirely unreadable: every later gate (add/commit/
            # rollback confirmation) would also be blind, so refuse even WITH the
            # opt-in rather than run writes against an unobservable device.
            raise UpgradeAbort(
                "Could not read Cisco-IOS-XE-install-oper data; RESTCONF may lack "
                "the install model. The operational gates cannot function — refusing."
            )
        # Collect the boot mode of EVERY member. The leaf is 'boot-mode' (17.15.x:
        # typedef install-boot-mode, values install-boot-mode-{unknown,install,
        # bundle}); older texts also describe an install-mode-* enum family. We
        # scope to oper-state containers first so stale rollback-point entries
        # can't pollute the read, and normalize both enum prefixes.
        suffixes = _boot_mode_suffixes(data)
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
                suffixes,
                extra=log,
            )
            return
        unconfirmed = [s for s in suffixes if s not in ("install", "install-bundle", "unknown")]
        if unconfirmed:
            # Present but unrecognized — fail closed regardless of the opt-in.
            raise UpgradeAbort(
                f"Unrecognized boot mode(s) {unconfirmed}; refusing (fail-closed). "
                "Verify the device is in install mode."
            )
        # No boot-mode leaf found, or the device reports the explicit 'unknown'
        # enum. Every supported release publishes this leaf (17.5.1+), so this
        # firing means leaf-name drift on a new release/platform — a situation
        # worth STOPPING for, not asserting through (the former
        # assume_install_mode override was removed deliberately).
        detail = f"read: {suffixes}" if suffixes else "no boot-mode value found"
        raise UpgradeAbort(
            f"Boot mode unconfirmed in install-oper ({detail}). Verify the device "
            "is in install mode ('show version'), and if it is, this release "
            "likely renamed the leaf — add the new name to BOOT_MODE_KEYS in "
            "constants.py and report it as an issue."
        )

    def _resolve_image(self, device, target_version, log):
        # Core precedence: a device-level image override wins, then an image mapped
        # to this device's device-type (the compatibility map), then the version's
        # default image.
        dev_images = list(device.software_image_files.filter(software_version=target_version))
        image = self._pick_image(dev_images)
        if image is not None:
            self.logger.info(
                "Using device-assigned image override '%s'.", image.image_file_name, extra=log
            )
        if image is None:
            dt_images = list(
                device.device_type.software_image_files.filter(software_version=target_version)
            )
            image = self._pick_image(dt_images)
        all_images = list(SoftwareImageFile.objects.filter(software_version=target_version))
        if image is None:
            image = self._pick_image(all_images, require_default=True)
            if image is not None:
                self.logger.warning(
                    "No image mapped to device-type '%s' for %s; using default image "
                    "'%s'. Verify it is correct for this platform.",
                    device.device_type,
                    target_version,
                    image.image_file_name,
                    extra=log,
                )
        if image is None:
            # Diagnostic abort: say exactly which link is missing so "it all looks
            # OK" cases are self-explaining in the Job Result.
            mappings = {
                img.image_file_name: [str(dt) for dt in img.device_types.all()]
                for img in all_images
            }
            defaults = [img.image_file_name for img in all_images if img.default_image]
            raise UpgradeAbort(
                f"No usable Software Image File for {target_version} (platform: "
                f"{target_version.platform}). Checked, in order: device-assigned "
                f"images (none for this version), device-type map for "
                f"'{device.device_type}' (image→device-type mappings on this "
                f"version: {mappings or 'none'}), and a default image (defaults: "
                f"{defaults or 'none'}). {len(all_images)} image record(s) exist for "
                "this exact version record. Fix: map an image to this device type "
                "or mark one as the default image (e.g. re-run 'Register IOS-XE "
                "Image' with Device types filled and/or Default image checked). If "
                "you expected a different version record, check for duplicate "
                "SoftwareVersion entries with the same version string on another "
                "platform."
            )
        if not image.image_file_name:
            raise UpgradeAbort(f"Image '{image}' has no image file name set in Nautobot.")
        if not image.download_url:
            raise UpgradeAbort(
                f"Image '{image.image_file_name}' has no download URL; the device "
                "needs a URL to pull the image from."
            )
        # Version/image consistency gate: rebuild letters are identity, so a
        # SoftwareVersion of '17.15.4' mapped to a 17.15.04a image would key
        # every downstream gate on the WRONG variant — worst case activating a
        # stale base image and reporting success while the registered rebuild
        # was never installed. Catch the mismatch BEFORE any device write.
        image_key = _version_key(image.image_file_name)
        target_key = _version_key(target_version.version)
        if image_key and target_key and image_key != target_key:
            raise UpgradeAbort(
                f"Version/image mismatch: target SoftwareVersion is "
                f"'{target_version.version}' but the resolved image file "
                f"'{image.image_file_name}' embeds a different version — rebuild "
                "letters count (17.15.4a is not 17.15.4). Fix the SoftwareVersion "
                "string or map the correct image, then re-run."
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

    def _discover_target_fs(self, client, log):
        """The device's writable install filesystem, read from the DEVICE.

        Catalyst switches call it 'flash'; IOS-XE routers (Catalyst 8000V)
        call it 'bootflash'. Rather than configuring this per platform, ask
        q-filesystem which partitions actually exist and pick the first
        supported candidate — an API answer, not an inference. Fail closed
        when nothing matches: every later step (space gate, copy destination,
        install add path) depends on it.
        """
        data = self._read_partitions(client) or {}
        names = {
            str(partition.get("name", "")).strip().rstrip(":").lower()
            for partition in _find_partitions(data)
        }
        for candidate in C.TARGET_FS_CANDIDATES:
            if any(
                name == candidate
                or name.startswith(candidate + "-")
                or name.startswith(candidate + ":")
                for name in names
            ):
                target_fs = f"{candidate}:"
                if candidate != C.TARGET_FS_CANDIDATES[0]:
                    self.logger.info(
                        "Target filesystem discovered from the device: %s",
                        target_fs,
                        extra=log,
                    )
                return target_fs
        raise UpgradeAbort(
            "Could not identify the target filesystem: the device's "
            f"q-filesystem partitions ({sorted(names) or 'none readable'}) "
            f"match none of {C.TARGET_FS_CANDIDATES}. If this platform names "
            "its writable filesystem differently, add it to "
            "TARGET_FS_CANDIDATES in constants.py."
        )

    def _gate_free_space(self, client, image, log, target_fs):
        # 'install add' distributes packages to EVERY stack member, so the gate
        # is the MINIMUM free space across all matching partitions of the
        # DISCOVERED filesystem (one per member on a stack; exactly one on a
        # standalone switch or a C8000V).
        data = self._read_partitions(client)
        frees = _flash_frees(data or {}, (target_fs.rstrip(":"),))
        if not frees:
            raise UpgradeAbort(
                f"Could not confirm free space on {target_fs} over RESTCONF "
                "even though the partition was just discovered — transient "
                "read failure? Refusing to copy without confirming space."
            )
        free = min(f for _, f in frees)
        if len(frees) > 1:
            self.logger.info(
                "Flash free space per member: %s — gating on the minimum.",
                {name: f for name, f in frees},
                extra=log,
            )
        size = image.image_file_size
        if size:
            needed = math.ceil(size * C.SPACE_HEADROOM_FACTOR)
            label = f"{size} bytes x{C.SPACE_HEADROOM_FACTOR} headroom"
        else:
            needed = C.SPACE_FALLBACK_MIN_BYTES
            label = f"{needed} bytes (image size unknown in Nautobot)"
            self.logger.warning(
                "Image file size not set in Nautobot; using fallback space requirement.",
                extra=log,
            )
        if free < needed:
            raise UpgradeAbort(
                f"Insufficient free space: {free} bytes free "
                f"({'minimum across members' if len(frees) > 1 else 'flash'}), need "
                f"{needed} ({label}). Run 'install remove inactive' or clean up flash."
            )
        self.logger.info(
            "Free-space gate passed (%s bytes free, need %s).", free, needed, extra=log
        )

    @staticmethod
    def _read_q_filesystem(client, retries=None):
        """GET q-filesystem data, retrying briefly on transient RESTCONF errors.

        Returns None only if every attempt fails (so a one-off blip after a
        multi-minute copy doesn't get mistaken for 'no data'). Callers that are
        already polling on their own cadence (the copy watcher) pass retries=1.
        """
        attempts = C.QFS_READ_RETRIES if retries is None else retries
        for attempt in range(attempts):
            try:
                return client.get(C.DATA_Q_FILESYSTEM, ok_404=True) or {}
            except RestconfError:
                if attempt + 1 >= attempts:
                    return None
                time.sleep(C.POLL_INTERVAL)
        return None

    def _read_partitions(self, client, retries=None):
        """Partition stats ONLY (name/total/used) — never the per-file listing.

        Uses RESTCONF `fields` sub-selection so the device does not walk the
        filesystem to build partition-content (that walk is what sprays smand
        SELinux AVC denials on the console). A release that rejects or ignores
        `fields` falls back to the full read — the parsers handle either shape.
        """
        try:
            return (
                client.get(
                    f"{C.DATA_Q_FILESYSTEM}?fields={C.QFS_PARTITIONS_FIELDS}",
                    ok_404=True,
                )
                or {}
            )
        except RestconfError as exc:
            # fields unsupported or a transient error — the full read (with
            # its own retry policy) is the fallback; parsers handle either
            # shape. NEVER silent (review finding): the fields read is the
            # AVC mitigation for partition stats, and losing it must leave a
            # breadcrumb naming the rejected expression.
            self.logger.warning(
                "Fields-scoped partitions read rejected by this release (%s) "
                "— falling back to a full q-filesystem read (expect smand AVC "
                "console noise). Rejected fields expression: %s",
                exc,
                C.QFS_PARTITIONS_FIELDS,
            )
            return self._read_q_filesystem(client, retries=retries)

    @staticmethod
    def _ledger_mount_root(client, target_fs):
        """The target media's mount root, from the device's OWN install records.

        Three state signals, strongest first, all in the one install-oper-data
        document this job already reads:
          * install-packages rows' pkg-dir — a LIST KEY, so it is always
            populated on an install-mode device, and device-derived: the
            expanded packages live at the boot media root;
          * '/'-rooted src-filename values (the recorded install source of a
            version) — their directory;
          * the most recent add record's dest-dir echo — populated only when
            the add itself DOWNLOADED the package. This job copies first and
            adds from local media, and local adds download nothing, so this
            echo is typically absent here.
        Live package rows are accepted as-is (the install-mode gate already
        proved from device state that this upgrade targets the boot media,
        and real mount roots need not resemble the filesystem name — a field
        9300 mounts flash: at /mnt/sd3/user). Echo evidence is accepted only
        when its last path component matches the target filesystem name:
        echoes record where an operator once pulled a file from, which may
        be other media entirely.

        Returns (root_or_None, info, error_or_None). info carries the raw
        values seen per signal — including historical package rows and
        non-absolute forms that can never become the hypothesis — so the
        caller can say precisely what the device published; error is set when
        the ledger could not be read at all — NOT the same as an empty
        history.
        """
        try:
            data = client.get(C.DATA_INSTALL_OPER, ok_404=True)
        except RestconfError as exc:
            return None, {}, f"install history read failed: {exc}"
        if data is None:
            return None, {}, "the install-oper endpoint returned 404"
        pkg_dirs = []
        hist_pkg_dirs = []  # historical rows: never a hypothesis, shown in diagnostics
        src_roots = []
        add_dirs = []  # (start-time, dir): recency decides within this signal
        raw_odd = []  # non-absolute values, reported verbatim on failure

        def _keep(pool, text):
            if text.startswith("/"):
                pool.append(text.rstrip("/") or "/")
            elif text and text not in raw_odd:
                raw_odd.append(text)

        def _walk(node, start_time=""):
            if isinstance(node, dict):
                stamp = str(node.get("start-time", start_time))
                # Live install-packages rows form the hypothesis (key:
                # pkg-dir pkg-name). The two HISTORICAL pkg-dir-keyed lists —
                # version state rows and rollback-point rows — both carry
                # package-type in their key (schema-verified across
                # 17.12/17.15/17.18/26.1); its absence positively identifies
                # a live row. Historical values are still RECORDED so failure
                # diagnostics can say exactly what the device published.
                if "pkg-dir" in node and "pkg-name" in node:
                    text = str(node.get("pkg-dir", "")).strip()
                    if "package-type" in node:
                        if text and text not in hist_pkg_dirs:
                            hist_pkg_dirs.append(text)
                    else:
                        _keep(pkg_dirs, text)
                # src-filename ALSO appears in add-param records — a verbatim
                # echo of whatever path the operator typed (review finding).
                # Only version rows (they carry version/src-checksum) count.
                if "version" in node or "src-checksum" in node:
                    src = str(node.get("src-filename", "")).strip()
                    if src.startswith("/") and "/" in src[1:]:
                        src_roots.append(src.rsplit("/", 1)[0] or "/")
                    elif src and src not in raw_odd:
                        raw_odd.append(src)
                add_param = node.get("add-param")
                if isinstance(add_param, dict):
                    text = str(add_param.get("dest-dir", "")).strip()
                    if text.startswith("/"):
                        add_dirs.append((stamp, text.rstrip("/") or "/"))
                    elif text and text not in raw_odd:
                        raw_odd.append(text)
                for value in node.values():
                    _walk(value, stamp)
            elif isinstance(node, list):
                for item in node:
                    _walk(item, start_time)

        _walk(data)
        info = {
            "pkg-dir": sorted(set(pkg_dirs)),
            "historical-pkg-dir": sorted(set(hist_pkg_dirs)),
            "src-root": sorted(set(src_roots)),
            "add-dest-dir": sorted({d for _, d in add_dirs}),
            "other": raw_odd,
        }
        wanted = str(target_fs).rstrip(":/").lower()

        def _named(pool):
            return [d for d in pool if d.rsplit("/", 1)[-1].lower() == wanted]

        # Live install-packages pkg-dir IS the boot-media mount root: the
        # install-mode gate confirmed boot MODE from device state, and on
        # every supported platform the discovered target filesystem
        # (flash:/bootflash:) is the boot media where packages live — so it
        # is accepted with NO name requirement. Field-proven necessity: a
        # real 9300 mounts flash: at /mnt/sd3/user, which a name gate wrongly
        # rejected (lab evidence 2026-07-10 — the watcher then observed the
        # file at exactly the address these rows had published). The MAJORITY
        # of live rows decides (the device's own dominant answer); a name
        # match only breaks ties — the same field evidence shows real roots
        # need not resemble the filesystem name, so one aliased minority row
        # must not outvote the rest. Still only a HYPOTHESIS tier: downstream
        # answers are corroborated or corrected by observation (learn/heal,
        # confirmed pre-check, full-listing verify), so a wrong root costs a
        # corrective walk, not the run.
        if pkg_dirs:
            counts = Counter(pkg_dirs)
            best = max(counts.values())
            leaders = [d for d in dict.fromkeys(pkg_dirs) if counts[d] == best]
            root = (_named(leaders) or leaders)[0]
            return root, {"source": "install package records (pkg-dir)", **info}, None
        # ECHO evidence (version-row source paths, add dest-dir) stays
        # name-gated: echoes record where an operator once pulled a file
        # from — USB sticks, subdirectories, other media (review-confirmed
        # poisoning) — and nothing ties them to the target filesystem. No
        # acceptable evidence -> no hypothesis; the copy watcher's
        # first-sighting observation takes over.
        for label, pool in (
            ("installed-version source paths", src_roots),
            # most recent add first: the reviewed recency rule for echoes
            ("install add dest-dir echoes", [d for _, d in sorted(add_dirs, reverse=True)]),
        ):
            named = _named(pool)
            if named:
                return named[0], {"source": label, **info}, None
        return None, info, None

    def _locate_target_partition(self, client, target_fs):
        """((entry keys, partition name) or None, reason) for the target fs.

        Needed to build the keyed partition-content URL (nested list entries
        must be addressed through their ancestors' keys). Uses the
        partitions-scoped read — no file walk. The reason string states
        exactly what the device returned when unresolvable, so a lab log
        discriminates 'no such partition' from 'the listing carried no
        location keys' (a real 9300 omitted them until the fields selection
        asked for the key leaves explicitly).
        """
        data = self._read_partitions(client, retries=1)
        if not data:
            return None, "the partitions listing was unreadable"
        wanted = target_fs.rstrip(":").lower()
        best = None
        seen = []
        matched_keyless = False
        any_keyed = False
        # tolerate wrapper shapes: hunt for dicts carrying a 'partitions' list
        for entry in _find_qfs_entries(data):
            for partition in entry.get("partitions") or []:
                raw_name = str(partition.get("name", "")).strip()
                if raw_name and raw_name not in seen:
                    seen.append(raw_name)
                name = raw_name.rstrip(":").lower()
                exact = name == wanted
                # Stacks report member-suffixed names (flash-1, flash-2): a
                # suffix match engages the keyed path there too; exact wins.
                keys = tuple(str(entry.get(k, "")) for k in ("fru", "slot", "bay", "chassis"))
                complete = all(k != "" for k in keys)
                if complete:
                    any_keyed = True
                if exact or name.startswith(wanted + "-") or name.startswith(wanted + ":"):
                    if complete:
                        if exact:
                            return (keys, raw_name), ""
                        if best is None:
                            best = (keys, raw_name)
                    else:
                        matched_keyless = True
        if best is not None:
            return best, ""
        if matched_keyless:
            # Observation only — no cause attribution: this response may even
            # have come from the full-read fallback, where no fields
            # selection was in play (review finding).
            if any_keyed:
                return None, (
                    "partition name(s) matched but their entries lacked "
                    "complete fru/slot/bay/chassis location keys (other "
                    "entries in the same listing carried them)"
                )
            return None, (
                "partition name(s) matched but no entry in the listing "
                "carried complete fru/slot/bay/chassis location keys"
            )
        return None, f"no partition named like '{wanted}' (partitions seen: {seen})"

    def _read_file_size(self, client, image_file_name, ref_cell, log, retries=None):
        """Size of ONE file, via a keyed partition-content entry when possible.

        ref_cell is a per-copy MUTABLE dict: {"ref": (keys, pname, full_path)
        or None, "disabled": bool, "misses": int}. The keyed GET asks about
        exactly one path — no filesystem walk, no AVC spray. Semantics:
          * keyed 404 -> None (file not there yet — normal mid-copy) and
            "misses" increments so the watcher can self-heal a stale path;
          * keyed transport/schema error -> logged ONCE, "disabled" set, and
            every later read uses the full walk EXPLICITLY (the operator can
            see the mitigation is inert) — never a silent per-poll retry;
          * no ref / disabled -> the full read: today's exact behavior.
        """
        ref = None if ref_cell.get("disabled") else ref_cell.get("ref")
        if ref is not None:
            (fru, slot, bay, chassis), pname, full_path = ref
            url = (
                f"{C.DATA_Q_FILESYSTEM}={_uq(fru)},{_uq(slot)},{_uq(bay)},{_uq(chassis)}"
                f"/partitions={_uq(pname)}/partition-content={_uq(full_path)}"
            )
            try:
                data = client.get(url, ok_404=True)
                if data is None:
                    ref_cell["misses"] = ref_cell.get("misses", 0) + 1
                    return None  # keyed entry absent: file not there (yet)
                ref_cell["misses"] = 0
                return _file_size_bytes(data, image_file_name)
            except RestconfError as exc:
                ref_cell["disabled"] = True
                self.logger.warning(
                    "Keyed file-size read rejected by this release (%s) — "
                    "falling back to full q-filesystem reads for the rest of "
                    "this copy (expect smand AVC log noise on 17.15.x).",
                    exc,
                    extra=log,
                )
        data = self._read_q_filesystem(client, retries=retries)
        if data is None:
            return None
        return _file_size_bytes(data, image_file_name)

    def _resolve_file_ref(self, client, image_file_name, target_fs, log):
        """Build the keyed file reference once per copy (or None -> fallback).

        When the ledger cannot supply the mount root, the copy watcher learns
        the file's real full-path from its FIRST successful full read and
        switches to keyed reads for the remaining polls — so this returning
        None costs at most a couple of walks, not the whole run.
        """
        root, info, error = self._ledger_mount_root(client, target_fs)
        if error:
            self.logger.info(
                "Could not read the install ledger for the mount root (%s) — "
                "the copy watcher will learn the file's path from its first "
                "sighting instead.",
                error,
                extra=log,
            )
            return None
        if not root:
            absolute = sorted(
                set(
                    (info.get("pkg-dir") or [])
                    + (info.get("src-root") or [])
                    + (info.get("add-dest-dir") or [])
                )
            )
            odd = info.get("other") or []
            if absolute:
                odd_note = f" (non-absolute values also seen: {odd})" if odd else ""
                self.logger.info(
                    "The install ledger publishes directory evidence (%s) but "
                    "none of it matches the %s filesystem%s — the copy watcher "
                    "will learn the file's path from its first sighting "
                    "instead.",
                    absolute,
                    target_fs,
                    odd_note,
                    extra=log,
                )
                return None
            if odd:
                self.logger.info(
                    "The install ledger carries no absolute directory "
                    "evidence (non-path values seen: %s) — the copy watcher "
                    "will learn the file's path from its first sighting "
                    "instead.",
                    odd,
                    extra=log,
                )
            elif info.get("historical-pkg-dir"):
                self.logger.info(
                    "The install ledger exposes only HISTORICAL package rows "
                    "(%s) — no live install-packages rows and no usable "
                    "echoes; historical rows describe past versions and "
                    "rollback points, not the current media, so they never "
                    "form the hypothesis. The copy watcher will learn the "
                    "file's path from its first sighting instead.",
                    info["historical-pkg-dir"],
                    extra=log,
                )
            else:
                self.logger.info(
                    "The install ledger carries no directory evidence — "
                    "normal when every install add ran against local media "
                    "(local adds download nothing, so their records echo no "
                    "dest-dir) and no package rows are exposed. The copy "
                    "watcher will learn the file's path from its first "
                    "sighting instead.",
                    extra=log,
                )
            return None
        located, why = self._locate_target_partition(client, target_fs)
        if located is None:
            self.logger.info(
                "The %s partition could not be located in the partitions "
                "listing (%s) — the copy watcher will learn the file's path "
                "from its first sighting instead.",
                target_fs,
                why,
                extra=log,
            )
            return None
        keys, pname = located
        self.logger.info(
            "Mount root %s resolved from the device's %s — progress reads "
            "will use keyed queries instead of per-poll filesystem walks.",
            root,
            info.get("source", "install records"),
            extra=log,
        )
        return keys, pname, f"{root.rstrip('/')}/{image_file_name}"

    def _config_sync_state(self, client):
        """Has running-config been written to startup-config? Tri-state.

        Returns (state, detail, ticks): state True = startup-config is
        current, False = running-config changed after startup was last
        written, None = indeterminate; ticks is {"running": int, "startup":
        int_or_None} whenever the leaves were readable (kept even when state
        is None so a save can be verified by CHANGE — wrap-immune), else
        None. Source: the CISCO-CONFIG-MAN-MIB ccmHistory timestamps
        (device-published; present 17.9.1-26.1.1). The comparison uses
        startup-last-changed, NOT running-last-saved — the MIB counts ANY
        write (even 'show run' to a terminal) as 'saved', so only the startup
        write time proves persistence.

        The leaves are sysUpTime ticks (uint32 hundredths, wrap ~497 days).
        A leaf LARGER than the device's current sysUpTime must predate a wrap
        — comparisons would be unreliable, so that reads as indeterminate
        (checked against the same SMIv2 bridge; if sysUpTime is unreadable
        the plain comparison stands). A device that has not saved since boot
        reports startup=0 and legitimately reads 'unsaved'. The MIB data is
        served via the device's SNMP agent: with snmp-server unconfigured the
        GET returns empty -> indeterminate, never assumed in-sync.
        """
        try:
            data = client.get(C.DATA_CONFIG_MAN, ok_404=True)
        except RestconfError as exc:
            return None, f"config timestamps unreadable: {exc}", None
        if data is None:
            return None, "the device does not expose CISCO-CONFIG-MAN-MIB (404)", None

        def _find_key(node, key):
            if isinstance(node, dict):
                if key in node:
                    return node
                for value in node.values():
                    found = _find_key(value, key)
                    if found is not None:
                        return found
            elif isinstance(node, list):
                for item in node:
                    found = _find_key(item, key)
                    if found is not None:
                        return found
            return None

        hist = _find_key(data, "ccmHistoryRunningLastChanged")
        if hist is None:
            return (
                None,
                "config timestamps empty — the MIB bridge needs the SNMP "
                "agent enabled (snmp-server) to answer",
                None,
            )
        try:
            running = int(hist["ccmHistoryRunningLastChanged"])
            raw_startup = hist.get("ccmHistoryStartupLastChanged")
            startup = None if raw_startup is None else int(raw_startup)
        except (KeyError, TypeError, ValueError):
            return None, f"config timestamps unparsable: {hist}", None
        ticks = {"running": running, "startup": startup}
        # Wrap check from state: any tick beyond current uptime predates a
        # sysUpTime wrap. Best-effort — an unreadable sysUpTime leaves the
        # plain comparison in force (the save verify is wrap-immune anyway).
        uptime = None
        try:
            sysdata = client.get(C.DATA_SNMP_SYSTEM, ok_404=True)
        except RestconfError:
            sysdata = None
        entry = _find_key(sysdata, "sysUpTime") if sysdata else None
        if entry is not None:
            try:
                uptime = int(entry["sysUpTime"])
            except (TypeError, ValueError):
                uptime = None
        if uptime is not None and max(running, startup or 0) > uptime + 500:
            return (
                None,
                f"config timestamps predate a sysUpTime counter wrap "
                f"(uptime tick {uptime}, running-config tick {running}, "
                f"startup tick {startup}) — tick comparison unreliable on "
                f"~497-day+ uptimes",
                ticks,
            )
        detail = (
            f"running-config last changed at tick {running}, "
            f"startup-config last written at tick "
            f"{startup if startup is not None else '0 (never since boot)'}"
        )
        return (startup or 0) >= running, detail, ticks

    def _save_config(self, client, log, pre_ticks=None):
        """Write running-config to startup-config; verify from the timestamps.

        Operator asked for the save (checkbox), so a refused save ABORTS
        (clean-before precedent: a requested precondition that cannot be
        delivered must not be silently skipped). Verification is state-based:
        the PRIMARY proof is the startup tick CHANGING between the pre-save
        read (pre_ticks) and the post-save read — immune to the ~497-day
        sysUpTime tick wrap that makes plain tick comparison lie (review
        finding: the naive predicate-as-gate aborted verified-good saves on
        year-plus uptimes). Abort only on POSITIVE evidence the save did not
        take (startup tick readable on both sides and unchanged while the
        config still reads dirty); when the timestamps cannot answer, the
        device's own result string is the labeled fallback.
        """
        self.logger.info(
            "Saving running-config to startup-config (cisco-ia:save-config)...",
            extra=log,
        )
        try:
            response = client.post_rpc(C.OP_SAVE_CONFIG, {})
        except RestconfError as exc:
            raise UpgradeAbort(
                f"'Save running-config before reload' was requested but the "
                f"save-config RPC failed: {exc}. Save manually ('write memory') "
                "and re-run, or untick the option."
            ) from exc
        error_text = _rpc_error_text(response)
        if error_text:
            raise UpgradeAbort(
                f"'Save running-config before reload' was requested but the "
                f"device rejected the save: {error_text}."
            )
        result = ""
        if isinstance(response, dict):
            for value in response.values():
                if isinstance(value, dict) and "result" in value:
                    result = str(value["result"])
                    break
        if "fail" in result.lower() or "error" in result.lower():
            raise UpgradeAbort(
                f"'Save running-config before reload' was requested but the "
                f"device reported: {result}."
            )
        state, detail, ticks = self._config_sync_state(client)
        pre_startup = (pre_ticks or {}).get("startup")
        post_startup = (ticks or {}).get("startup")
        if state is True:
            self.logger.info(
                "Running-config saved to startup-config and verified from the "
                "device's config timestamps (%s).",
                detail,
                extra=log,
            )
        elif post_startup is not None and pre_startup is not None and post_startup != pre_startup:
            # Wrap-immune: the startup timestamp MOVED across the save — the
            # write landed even where raw tick comparison misreads.
            self.logger.info(
                "Running-config saved to startup-config — verified by the "
                "startup timestamp advancing (tick %s -> %s).",
                pre_startup,
                post_startup,
                extra=log,
            )
        elif post_startup is not None and pre_startup is not None:
            # Both sides readable, startup tick UNCHANGED, config still reads
            # dirty: positive evidence the save did not take.
            raise UpgradeAbort(
                f"save-config reported success ({result or 'empty result'}) but "
                f"the startup-config timestamp did not move (tick {post_startup} "
                f"before and after) and the config still reads unsaved ({detail}) "
                "— refusing to reload past a save that did not take."
            )
        else:
            self.logger.info(
                "Running-config saved (device result: %s). Timestamp "
                "verification unavailable (%s) — trusting the device's result "
                "string as the fallback.",
                result or "empty",
                detail,
                extra=log,
            )

    def _member_roster(self, client):
        """Stack member roster: {(hw-dev-index, serial)} for chassis entries.

        Returns None when the inventory is unreadable or carries no chassis
        entries (the caller then skips the completeness check rather than
        guessing).
        """
        try:
            data = client.get(C.DATA_DEVICE_INVENTORY, ok_404=True) or {}
        except RestconfError:
            return None
        roster = set()
        for entry in _find_inventory_entries(data):
            if "chassis" in str(entry.get("hw-type", "")).lower():
                roster.add(
                    (
                        str(entry.get("hw-dev-index", "?")),
                        str(entry.get("serial-number", "")).strip(),
                    )
                )
        return roster or None

    def _verify_members(self, client, roster, log):
        """Require every pre-upgrade stack member to rejoin before committing.

        Without this, a member that fails to boot after the reload would go
        unnoticed: the active reports the target version, the job commits, and
        the stack silently loses a member. Members can come up staggered, so
        poll up to MEMBER_CHECK_TIMEOUT before refusing.
        """
        if not roster:
            self.logger.info(
                "Stack member roster was not readable before the upgrade; "
                "skipping the member-completeness check.",
                extra=log,
            )
            return
        deadline = time.monotonic() + C.MEMBER_CHECK_TIMEOUT
        polls = 0
        current = set()
        while time.monotonic() < deadline:
            self._check_stop()
            current = self._member_roster(client) or set()
            if current >= roster:
                self.logger.info(
                    "All %d stack member(s) rejoined after the reload.",
                    len(roster),
                    extra=log,
                )
                return
            polls += 1
            if polls % 4 == 0:  # heartbeat every ~2 minutes
                self.logger.info(
                    "Waiting for stack members to rejoin (missing: %s)...",
                    sorted(roster - current),
                    extra=log,
                )
            time.sleep(C.POLL_INTERVAL)
        raise UpgradeAbort(
            f"Stack member(s) missing after the reload: {sorted(roster - current)} "
            f"(present: {sorted(current) or 'none readable'}). NOT committing — the "
            "auto-rollback timer should revert the stack; investigate the missing "
            "member(s) before re-running."
        )

    # ------------------------------------------------- helpers: device writes --

    def _copy_image(self, client, image, log, target_fs):
        """Run the classic copy RPC in a worker thread and watch its progress.

        The classic Cisco-IOS-XE-rpc:copy BLOCKS for the whole transfer (chosen
        deliberately: a real 17.15.05 silently broke the fancier async xcopy
        while this path kept working). Running it in a thread frees the job to
        poll the growing on-device file for progress lines; the final exact size
        match is the transfer-integrity gate, and 'install add' image-signature
        validation remains the cryptographic backstop.
        """
        dest = f"{target_fs}{image.image_file_name}"
        expected = image.image_file_size
        # Resolve the keyed file reference ONCE (ledger dest-dir + partition
        # location): every file-size read this copy makes — pre-check, the
        # ~30 progress polls, the final verify — then asks the device about
        # exactly one path instead of requesting a full filesystem walk.
        ref_cell = {
            "ref": self._resolve_file_ref(client, image.image_file_name, target_fs, log),
            "disabled": False,
            "misses": 0,
        }
        pre_size = self._read_file_size(client, image.image_file_name, ref_cell, log)
        pre_context = None
        if pre_size is None and ref_cell.get("ref") is not None and not ref_cell.get("disabled"):
            # A keyed 404 is NOT positive evidence of absence — the address
            # is a ledger-derived hypothesis. One full listing decides:
            # genuinely absent, or present at a DIFFERENT address (then the
            # observation corrects the hypothesis, exactly like the watcher's
            # heal tier — review finding: a wrong-but-accepted root otherwise
            # silently defeats the idempotent skip and re-copies a multi-GB
            # image, and suppresses the overwrite warning).
            data = self._read_q_filesystem(client) or {}
            pre_context = _find_file_context(data, image.image_file_name)
            if pre_context is not None:
                keys, pname, entry = pre_context
                full_path = str(entry.get("full-path", ""))
                if full_path.startswith("/") and keys is not None and pname is not None:
                    ref_cell["ref"] = (keys, pname, full_path)
                    ref_cell["misses"] = 0
                    self.logger.info(
                        "Corrected the on-device file address from a full "
                        "listing (%s) — the ledger-derived path had no entry.",
                        full_path,
                        extra=log,
                    )
                try:
                    pre_size = int(entry.get("file-size") or entry.get("size"))
                except (TypeError, ValueError):
                    pre_size = None
        if expected and pre_size is not None and pre_size == expected:
            if pre_context is not None:
                # Size AND address both just came from the full listing —
                # already observation-corroborated; no second walk needed.
                self.logger.info(
                    "Image already present on %s with the expected size (%s bytes); "
                    "skipping copy (address and size confirmed by a full listing).",
                    dest,
                    expected,
                    extra=log,
                )
                return
            # Idempotent short-circuit — but a SKIP is consequential, and the
            # keyed hit rests on a ledger-derived path hypothesis. Spend ONE
            # authoritative full read and require it to corroborate the SAME
            # address (a stale subdirectory or another stack member can hold a
            # same-named exact-size file — basename-anywhere matching would
            # rubber-stamp the very hit it is meant to check; review finding).
            # Known limit: q-filesystem does not say which member owns flash:
            # after a failover, so a wrong-member skip still fails loudly at
            # 'install add' (file not found) rather than silently.
            data = self._read_q_filesystem(client) or {}
            context = _find_file_context(data, image.image_file_name)
            confirmed = None
            confirmed_path = ""
            if context is not None:
                try:
                    confirmed = int(context[2].get("file-size") or context[2].get("size"))
                except (TypeError, ValueError):
                    confirmed = None
                confirmed_path = str(context[2].get("full-path", ""))
            ref = ref_cell.get("ref")
            same_address = ref is None or (ref is not None and confirmed_path == ref[2])
            if confirmed == expected and same_address:
                if confirmed_path.startswith("/") and context[0] is not None:
                    ref_cell["ref"] = (context[0], context[1], confirmed_path)
                self.logger.info(
                    "Image already present on %s with the expected size (%s bytes); "
                    "skipping copy (address and size confirmed by a full listing).",
                    dest,
                    expected,
                    extra=log,
                )
                return
            self.logger.warning(
                "Keyed read reported the image present at the expected size, but "
                "the full listing does not corroborate it (size: %s, path: %s) — "
                "copying anyway; the ledger-derived path may be stale.",
                confirmed if confirmed is not None else "file not found",
                confirmed_path or "none",
                extra=log,
            )
        if pre_size is not None:
            self.logger.warning(
                "A file named %s already exists on flash (%s bytes, expected %s); "
                "it will be overwritten by the copy.",
                image.image_file_name,
                pre_size,
                expected or "unknown",
                extra=log,
            )
        self._preflight_download_url(image.download_url, log)
        self.logger.info(
            "Starting copy to %s (expected size: %s)...",
            dest,
            f"{expected} bytes" if expected else "unknown",
            extra=log,
        )
        payload = {
            "Cisco-IOS-XE-rpc:input": {
                "source-drop-node-name": image.download_url,
                "destination-drop-node-name": dest,
            }
        }
        result = {}

        def _do_copy():
            try:
                result["response"] = client.post_rpc(C.OP_COPY, payload, timeout=C.COPY_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - surfaced by the watcher
                result["error"] = exc

        copy_thread = threading.Thread(target=_do_copy, daemon=True, name="iosxe-copy")
        copy_thread.start()
        # Poll from a SEPARATE session: requests.Session is not thread-safe and
        # the original one is occupied by the blocking copy.
        self._watch_copy(
            client.clone(), copy_thread, result, image, expected, log, target_fs, ref_cell
        )

    def _watch_copy(
        self, poll_client, copy_thread, result, image, expected, log, target_fs, ref_cell
    ):
        """Report progress while the copy thread runs; verify when it finishes.

        Progress is best-effort (some releases may not list the growing file);
        there is deliberately NO stall abort — the blocking RPC itself is the
        liveness signal: server/TLS failures return quickly as errors, and hangs
        are bounded by COPY_TIMEOUT.
        """
        started = time.monotonic()
        deadline = started + C.COPY_TIMEOUT + 2 * C.POLL_INTERVAL
        last_logged = 0
        polls = 0
        while copy_thread.is_alive():
            self._check_stop()
            if time.monotonic() > deadline:
                raise UpgradeAbort(
                    f"Copy did not complete within {C.COPY_TIMEOUT}s. "
                    + _fetch_hints(image.download_url)
                )
            time.sleep(C.POLL_INTERVAL)
            polls += 1
            heal_now = (
                ref_cell.get("ref") is not None
                and not ref_cell.get("disabled")
                and ref_cell.get("misses", 0) > 0
                and ref_cell["misses"] % 4 == 0
            )
            if ref_cell.get("ref") is not None and not ref_cell.get("disabled") and not heal_now:
                size = self._read_file_size(
                    poll_client, image.image_file_name, ref_cell, log, retries=1
                )
            else:
                # Full read for one of three reasons: no ref yet (LEARN the
                # real path from the first sighting), keyed reads disabled
                # (release rejected them — explicit fallback), or HEAL (the
                # keyed path has 404ed for ~2 minutes straight while the copy
                # runs — the ledger-derived hypothesis may be stale, so check
                # what the device actually shows and correct the address from
                # OBSERVATION).
                data = self._read_q_filesystem(poll_client, retries=1) or {}
                context = _find_file_context(data, image.image_file_name)
                size = None
                if context is not None:
                    keys, pname, entry = context
                    try:
                        size = int(entry.get("file-size") or entry.get("size"))
                    except (TypeError, ValueError):
                        size = None
                    full_path = str(entry.get("full-path") or "")
                    if (
                        not ref_cell.get("disabled")
                        and full_path.startswith("/")
                        and keys is not None
                        and pname is not None
                    ):
                        old_ref = ref_cell.get("ref")
                        new_ref = (keys, pname, full_path)
                        if old_ref != new_ref:
                            ref_cell["ref"] = new_ref
                            ref_cell["misses"] = 0
                            self.logger.info(
                                "%s the on-device file address (%s) — "
                                "remaining size reads use the keyed entry.",
                                "Corrected" if old_ref else "Learned",
                                full_path,
                                extra=log,
                            )
                        else:
                            ref_cell["misses"] = 0
                elif heal_now:
                    ref_cell["misses"] = 0  # file genuinely absent; keep keyed cadence
            elapsed = int(time.monotonic() - started)
            if size is not None:
                step = max((expected or 0) // 20, 25_000_000)  # ~5% or 25 MB
                if abs(size - last_logged) >= step:
                    last_logged = size
                    self.logger.info(
                        "Copy progress: %s (elapsed %ss).",
                        _progress_label(size, expected),
                        elapsed,
                        extra=log,
                    )
            elif polls % 4 == 0:  # heartbeat every ~2 minutes
                self.logger.info(
                    "Copy running (elapsed %ss; transfer size not yet visible on-device)...",
                    elapsed,
                    extra=log,
                )
        copy_thread.join(timeout=5)

        error = result.get("error")
        if error is not None:
            if isinstance(error, RestconfError):
                raise UpgradeAbort(_interpret_copy_failure(error, image.download_url)) from error
            raise UpgradeAbort(
                f"Copy failed unexpectedly: {error}. " + _fetch_hints(image.download_url)
            ) from error
        error_text = _rpc_error_text(result.get("response"))
        if error_text:
            raise UpgradeAbort(
                f"The copy was rejected by the device: {error_text}. "
                + _fetch_hints(image.download_url)
            )
        # RPC returned success — verify what actually landed on flash from
        # the authoritative full listing, ALWAYS. A keyed read here can 404
        # on a stale path (downgrading the abort gate to a warning) or, worse,
        # HIT a same-named file at a wrong address and fake exact agreement —
        # each review round produced one of those. One full walk per copy is
        # the price of an honest transfer-integrity gate; keyed reads keep
        # serving the ~30 progress polls where the AVC flood actually was.
        size = _file_size_bytes(self._read_q_filesystem(poll_client) or {}, image.image_file_name)
        elapsed = int(time.monotonic() - started)
        if expected and size is not None:
            if abs(size - expected) > C.SIZE_MATCH_TOLERANCE_BYTES:
                raise UpgradeAbort(
                    f"Copy finished but the on-device size is {size} bytes, expected "
                    f"{expected} — transfer incomplete or wrong file on the server."
                )
            self.logger.info(
                "Copy complete and size verified (%s bytes, %ss).", size, elapsed, extra=log
            )
        else:
            self.logger.warning(
                "Copy finished but the size could not be verified (on-device: %s, "
                "expected: %s); relying on 'install add' signature validation. Set "
                "the image file size in Nautobot for a stricter gate.",
                size if size is not None else "unreadable",
                expected or "unknown",
                extra=log,
            )

    def _preflight_download_url(self, url, log):
        """Sanity-check the download URL from the worker before starting the copy.

        The Register job validates via the worker's INTERNAL route, so the
        device-facing URL stored on the image may never have been exercised. A
        definitive 404/410 (server answered: the file is not there) aborts --
        e.g. never uploaded, renamed, or pruned. Anything else only warns: the
        worker's network path is not the device's, and TLS trust differs, so an
        unreachable-from-worker URL can still be perfectly fetchable on-device.
        """
        try:
            resp = requests.head(url, timeout=C.GET_TIMEOUT, verify=False, allow_redirects=True)
        except requests.RequestException as exc:
            self.logger.warning(
                "Could not verify the image URL from the worker (%s); the device "
                "may still reach it -- proceeding.",
                exc,
                extra=log,
            )
            return
        if resp.status_code in (404, 410):
            raise UpgradeAbort(
                f"The image URL returns HTTP {resp.status_code} -- the file is not "
                f"on the firmware server ({url}). Re-upload it via Filebrowser "
                "and/or re-run 'Register IOS-XE Image', then re-run this job."
            )
        if resp.ok:
            self.logger.info(
                "Image URL verified from the worker (HTTP %s, Content-Length: %s).",
                resp.status_code,
                resp.headers.get("Content-Length", "n/a"),
                extra=log,
            )
        else:
            self.logger.warning(
                "Image URL probe returned HTTP %s from the worker; proceeding -- "
                "the device may still be able to fetch it.",
                resp.status_code,
                extra=log,
            )

    def _install_add(self, client, image, op_uuid, log, target_fs):
        path = f"{target_fs}{image.image_file_name}"
        version_str = image.software_version.version
        # NOTE (verified on a real 17.15.4): install-version-state-in-progress
        # ("marked for activation") is the NORMAL resting state of an added,
        # not-yet-activated image — the CLI shows a clean 'I' state and 'install
        # abort' reports nothing to abort. It does NOT indicate a stuck
        # transaction; a pre-existing added/pending state just makes this add a
        # quick no-op.
        pre_tokens = self._state_tokens(client, version_str)
        pre_states = {_classify_state(t) for t in pre_tokens}
        if pre_states & {"pending", "added", "activated", "uncommitted", "committed"}:
            # Already added: issuing another add is refused by the engine ("super
            # package already added") and only litters the device log — skip it.
            self.logger.info(
                "Target version already added (state: %s); skipping install add.",
                sorted(pre_tokens),
                extra=log,
            )
            # The staged row can belong to an add still in its final phase (a
            # killed prior run / CLI add — version rows appear ~60-70s early),
            # so do NOT claim ledger-grade confidence: keep the settle tier
            # armed for releases without sys-activity.
            return False
        self._wait_for_engine_idle(client, log, "install add")
        self.logger.info("install add %s ...", path, extra=log)
        # INVARIANT (26.1.1+): 'path' sits inside the now-mandatory choice
        # install-type-by-choice (path | name) — an install without one of them
        # is rejected. Never send a uuid-only install.
        payload = {"Cisco-IOS-XE-install-rpc:input": {"uuid": op_uuid, "path": path}}
        response = client.post_rpc(C.OP_INSTALL, payload, timeout=C.RPC_TIMEOUT)
        if response:
            self.logger.info("install add RPC response: %s", response, extra=log)
        # The RPC returns 2xx even when the engine refuses the add (real-device
        # pattern); if the response body itself carries the failure, abort now —
        # do NOT let a residual state row vouch for a failed add.
        error_text = _rpc_error_text(response)
        if error_text:
            raise UpgradeAbort(
                f"install add was rejected by the install engine: {error_text} — "
                f"{self._staged_hint(client, version_str)}"
                "Check 'show install log' and flash:.installer logs. If leftover "
                "packages from a prior life of this version exist, run 'install "
                "remove inactive' and re-run this job."
            )
        # Ledger-primary completion: the add op is DONE only when every record
        # for our uuid completes (the version-state row appears at extract-done,
        # ~60-70s BEFORE the add's post-check phase finishes — gating on it
        # fired activates into a still-running add, which the engine drops).
        try:
            outcome = self._await_op(client, op_uuid, C.ADD_TIMEOUT, log, "install add")
        except LedgerOpFailure as exc:
            # The engine recorded WHY it failed; add WHAT it says is staged —
            # the "different version already staged" case in operator terms.
            hint = self._staged_hint(client, version_str)
            if hint:
                raise LedgerOpFailure(f"{exc} {hint}") from exc
            raise
        if outcome == "success":
            return True
        if outcome == "absent":
            self.logger.warning(
                "The operation ledger never listed the install add (uuid %s) — "
                "this release may not populate operation records. Falling back "
                "to install-state inference.",
                op_uuid,
                extra=log,
            )
        else:  # timeout with records still running — legacy check, then proceed
            self.logger.warning(
                "install add records did not complete in time; cross-checking "
                "via install state before proceeding (activation start is "
                "verified independently).",
                extra=log,
            )
        self._wait_for_added(client, version_str, log)
        return False

    def _wait_for_added(self, client, version_str, log):
        deadline = time.monotonic() + C.ADD_TIMEOUT
        started = time.monotonic()
        polls = 0
        while time.monotonic() < deadline:
            self._check_stop()
            # States that mean the add has finished (verified on a real 17.15.4):
            # 'pending' (install-version-state-in-progress = "marked for
            # activation") is the NORMAL resting state of an added image, and
            # installed/added or beyond likewise count. 'present' does NOT — it
            # means files-on-disk without DB staging (residual from a prior life
            # of this version) and must never confirm an add. While the add is
            # still extracting, the version is absent or sits at 'invalid' →
            # keep waiting.
            tokens = self._state_tokens(client, version_str)
            states = {_classify_state(t) for t in tokens}
            if states & {"pending", "added", "activated", "uncommitted", "committed"}:
                self.logger.info("install add complete (state: %s).", sorted(tokens), extra=log)
                return
            polls += 1
            if polls % 4 == 0:  # heartbeat every ~2 minutes
                self.logger.info(
                    "install add still running (state: %s, elapsed %ds of up to %ds)...",
                    sorted(tokens) or "not visible yet",
                    int(time.monotonic() - started),
                    C.ADD_TIMEOUT,
                    extra=log,
                )
            time.sleep(C.POLL_INTERVAL)
        # Timed out on absent/unclassifiable states only. Warn and proceed;
        # _confirm_activation aborts if the activation then never starts.
        final_tokens = self._state_tokens(client, version_str)
        self.logger.warning(
            "Could not confirm 'install add' completion for %s from install-oper "
            "within %ss (state: %s); the add may have failed silently — check "
            "flash:.installer logs if activation does not start. Proceeding to "
            "activate (activation start is verified).",
            version_str,
            C.ADD_TIMEOUT,
            sorted(final_tokens) or "none",
            extra=log,
        )

    def _inventory_other_versions(self, client, target_str):
        """(entries, staged): friendly install-DB summary of NON-target versions.

        Read fresh from the documented install-version-state-info rows; used to
        explain "different version already staged" situations in the operator's
        terms. Advisory only — a read failure returns empty rather than ever
        breaking the run.
        """
        try:
            data = client.get(C.DATA_INSTALL_OPER, ok_404=True) or {}
        except RestconfError:
            return [], []
        target = _version_key(target_str)
        labels = {
            "pending": "staged — marked for activation",
            "added": "added (available for activation)",
            "uncommitted": "activated, NOT committed",
            "committed": "committed",
            "present": "files present, not staged",
        }
        entries = []
        staged = []
        for row in _find_version_rows(data):
            version = str(row.get("version", "")).strip()
            if not version or _version_key(version) == target:
                continue
            state_class = _classify_state(str(row.get("version-state", "")))
            entries.append(
                f"{version}: {labels.get(state_class, str(row.get('version-state', '?')))}"
            )
            if state_class in ("pending", "added"):
                staged.append(version)
        return entries, staged

    def _staged_hint(self, client, target_str):
        """One sentence for failure messages naming what IS staged, with exits."""
        entries, staged = self._inventory_other_versions(client, target_str)
        if not staged:
            return ""
        return (
            f"The device reports: {'; '.join(entries)}. A different staged "
            f"version ({', '.join(staged)}) usually means another upgrade was "
            "already prepared on this device — verify no change is in flight "
            "before clearing it. To proceed with THIS version instead: if you "
            "own this device's change, re-run with 'Clean device first' ticked "
            "(or clear deliberately via CLI 'install remove inactive'); the "
            "Remove-inactive option runs only after a successful commit and "
            "does not clear staged images. "
        )

    def _check_stop(self):
        """Cooperative stop checkpoint (set when the job's time budget expires).

        Every polling loop calls this each iteration, so an in-flight device
        stops at a SAFE boundary — between steps, never mid-decision — within
        about one poll interval. The install gates (copy/add skip-if-done,
        commit-to-be-safe) make an idempotent re-run pick the device back up.
        """
        stop = getattr(self, "_stop", None)
        if stop is not None and stop.is_set():
            raise UpgradeAbort(
                "stopped by the job's time budget before the next step — the "
                "device was left at a safe boundary; re-run this job for it "
                "(the gates make re-runs safe)"
            )

    def _await_op(self, client, op_uuid, timeout, log, context):
        """Track an install RPC by its uuid in the engine's operation ledger.

        Primary completion signal (state over inference): the engine records
        every operation under the RPC-supplied uuid with per-phase statuses.
        Returns 'success' when every record for the uuid completes successfully,
        'absent' when the ledger never lists the uuid within LEDGER_ABSENT_POLLS
        polls (a silently dropped request, or a release that does not populate
        the ledger — the caller picks the fallback), or 'timeout' when records
        exist but never complete in time (the caller decides how fatal that is).
        A ledger-RECORDED failure raises with the engine's own phase detail.
        RestconfError propagates (reload-tolerant callers interpret it).
        """
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        polls = 0
        absent_streak = 0
        success_count = None
        detail = ""
        while time.monotonic() < deadline:
            self._check_stop()
            data = client.get(C.DATA_INSTALL_OPER, ok_404=True) or {}
            records = _find_op_records(data, op_uuid)
            status, detail = _classify_ops(records)
            if status == "success":
                # Corroborate before trusting it: one RPC yields MULTIPLE records
                # created moments apart (field-verified), so a poll can land after
                # record N completes but before record N+1 exists. Require the
                # engine to CONFIRM: sys-activity idle in the SAME read (when the
                # release reports it), plus a stable record count across two
                # consecutive polls.
                activities = [
                    str(v).strip().lower() for v in _find_all_values(data, "sys-activity")
                ]
                busy = sorted({a for a in activities if not a.endswith("no-activity")})
                if busy:
                    status, detail = "running", f"records complete but engine busy ({busy})"
                elif success_count == len(records):
                    self.logger.info(
                        "%s confirmed by the operation ledger (%s, stable across polls, %ds).",
                        context,
                        detail,
                        int(time.monotonic() - started),
                        extra=log,
                    )
                    return "success"
                else:
                    success_count = len(records)
            if status != "success":
                success_count = None
            if status == "failure":
                raise LedgerOpFailure(
                    f"{context} FAILED per the device's operation ledger: {detail} — "
                    "check 'show install log' and the flash:.installer logs for the "
                    "phase named above."
                )
            polls += 1
            if status == "absent":
                # CONSECUTIVE absences only: a single stale/empty read mid-run
                # (e.g. the record migrating between the running and history
                # lists) must not abandon ledger tracking.
                absent_streak += 1
                if absent_streak >= C.LEDGER_ABSENT_POLLS:
                    return "absent"
            else:
                absent_streak = 0
            if polls % 4 == 0:  # heartbeat every ~2 minutes
                self.logger.info(
                    "%s running (ledger phase: %s, elapsed %ds of up to %ds)...",
                    context,
                    detail,
                    int(time.monotonic() - started),
                    int(timeout),
                    extra=log,
                )
            time.sleep(C.POLL_INTERVAL)
        self.logger.warning(
            "%s not ledger-confirmed within %ds (last ledger state: %s).",
            context,
            int(timeout),
            detail or "unknown",
            extra=log,
        )
        return "timeout"

    def _wait_for_engine_idle(self, client, log, context, settle_fallback=False):
        """Positive coast-is-clear gate before every install-engine write.

        The engine SILENTLY drops requests that arrive while it is busy
        (field-verified: an activate landing inside the add's post-check phase
        never started an operation). The oper-state 'sys-activity' leaf reports
        exactly this (install-no-activity vs install-/issu-in-progress), so wait
        until EVERY member reports idle. When the release does not report the
        leaf: pre-activate with an unconfirmed add gets the fixed settle delay
        (the one place the drop is proven); other writes proceed immediately —
        their ledger tracking remains the arbiter. Still-busy at timeout also
        proceeds with a warning for the same reason.
        """
        deadline = time.monotonic() + C.ENGINE_IDLE_TIMEOUT
        started = time.monotonic()
        polls = 0
        empty_streak = 0
        last_seen = []
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                data = client.get(C.DATA_INSTALL_OPER, ok_404=True) or {}
            except RestconfError:
                data = {}  # transient read blip — treated like an empty read below
            activities = [str(v).strip().lower() for v in _find_all_values(data, "sys-activity")]
            if not activities:
                # One empty read proves nothing (transient 404/blip); conclude
                # "leaf not reported" only after two CONSECUTIVE empty reads.
                empty_streak += 1
                if empty_streak >= 2:
                    if settle_fallback:
                        self.logger.info(
                            "Engine activity (sys-activity) not reported by this "
                            "release; using the fixed settle delay (%ss) before %s.",
                            C.ACTIVATE_SETTLE_DELAY,
                            context,
                            extra=log,
                        )
                        time.sleep(C.ACTIVATE_SETTLE_DELAY)
                    return
                time.sleep(C.POLL_INTERVAL)
                continue
            empty_streak = 0
            last_seen = sorted(set(activities))
            if all(a.endswith("no-activity") for a in activities):
                if polls:
                    self.logger.info(
                        "Install engine idle after %ds — clear for %s.",
                        int(time.monotonic() - started),
                        context,
                        extra=log,
                    )
                return
            polls += 1
            if polls % 4 == 0:  # heartbeat every ~2 minutes
                self.logger.info(
                    "Waiting for the install engine to go idle before %s "
                    "(sys-activity: %s, elapsed %ds of up to %ds)...",
                    context,
                    last_seen,
                    int(time.monotonic() - started),
                    C.ENGINE_IDLE_TIMEOUT,
                    extra=log,
                )
            time.sleep(C.POLL_INTERVAL)
        # The device POSITIVELY reported busy the whole window: writing now would
        # be silently dropped (field-verified). State says do not write — refuse.
        raise UpgradeAbort(
            f"The install engine reported busy (sys-activity: {last_seen}) for "
            f"{C.ENGINE_IDLE_TIMEOUT}s before {context} — another install "
            "operation (CLI, or another job run) appears to be in progress. "
            "Refusing to write into a busy engine; re-run after it settles "
            "('show install summary')."
        )

    def _install_activate(self, client, image, op_uuid, log):
        # Resolve the FULL internal version identifier from the documented
        # install-version-state-info rows (e.g. '17.15.04.0.6839'):
        # activate-by-bare-version hangs the RPC when the target is a
        # previously-tracked image (re-activation/rollback), while the full
        # string works — verified by direct RPC experiments on a real 17.15.x.
        # Fresh adds work either way, so the full form is used always.
        data = client.get(C.DATA_INSTALL_OPER, ok_404=True) or {}
        bare = image.software_version.version
        full = _full_version_string(data, bare)
        if full and full != bare:
            self.logger.info(
                "Resolved activation target '%s' → '%s' (from install-oper).",
                bare,
                full,
                extra=log,
            )
        version = full or bare
        self.logger.info(
            "install activate %s (explicitly non-ISSU) → device reloads...",
            version,
            extra=log,
        )
        # issu=false explicitly (a real 17.15.4 fatally failed activation on an
        # "ISSU compatibility check" when the request was ambiguous); no
        # auto-abort-timer-val — the platform's default rollback timer applies and
        # is verified after reload by _log_rollback_state.
        payload_input = {"uuid": op_uuid, "version": version, "issu": False}
        response = client.post_rpc(
            C.OP_ACTIVATE,
            {"Cisco-IOS-XE-install-rpc:input": dict(payload_input)},
            timeout=C.RPC_TIMEOUT,
            tolerate_disconnect=True,
        )
        # The RPC returns 2xx even when the install engine rejects the request
        # (e.g. 'add in progress' — seen on a real 17.15.4), so surface whatever
        # the body says and NEVER trust the status code alone; _confirm_activation
        # below is the actual gate.
        if response and response.get("_timeout"):
            self.logger.warning(
                "activate RPC did not return within %ss — the connection stayed "
                "open, so this is a STUCK engine call, not a reload. The ledger "
                "tracking below decides what actually happened.",
                C.RPC_TIMEOUT,
                extra=log,
            )
        elif response and not response.get("_disconnected"):
            self.logger.info("activate RPC response: %s", response, extra=log)
            error_text = _rpc_error_text(response)
            if error_text:
                raise UpgradeAbort(
                    f"install activate was rejected by the install engine: "
                    f"{error_text} — check 'show install log' and flash:.installer "
                    "logs; the device is unchanged."
                )

        def resend():
            # Re-issue with the SAME uuid: it is the ledger tracking key, and a
            # duplicate is harmless — a rejection body ('already in progress')
            # means an earlier request finally took; the ledger stays the arbiter.
            try:
                retry_response = client.post_rpc(
                    C.OP_ACTIVATE,
                    {"Cisco-IOS-XE-install-rpc:input": dict(payload_input)},
                    timeout=C.RPC_TIMEOUT,
                    tolerate_disconnect=True,
                )
            except RestconfError as exc:
                self.logger.warning("activate re-send failed: %s", exc, extra=log)
                return
            if retry_response and not (
                retry_response.get("_disconnected") or retry_response.get("_timeout")
            ):
                self.logger.info("activate re-send RPC response: %s", retry_response, extra=log)

        return resend

    def _confirm_activation(self, client, version_str, op_uuid, log, resend):
        """Verify the activation actually started, tracking it in the ledger.

        Evidence, in order of authority (state over inference):
          * the device drops offline -> the reload is under way (success);
          * the ledger records a FAILURE for our uuid -> abort with the phase;
          * install state turns activated/uncommitted/committed -> confirmed;
          * the ledger lists our op -> engaged; keep waiting for the reload;
          * the ledger stays ABSENT for LEDGER_ABSENT_POLLS polls -> the engine
            dropped the request (field-verified failure mode) -> re-send the
            SAME request and keep tracking. If nothing ever registers, abort
            with the device unchanged.
        """
        deadline = time.monotonic() + C.ACTIVATE_START_TIMEOUT
        started = time.monotonic()
        polls = 0
        absent_streak = 0
        resends = 0
        engaged = False
        last_detail = ""
        last_tokens = []
        while time.monotonic() < deadline:
            self._check_stop()
            time.sleep(C.POLL_INTERVAL)
            try:
                data = client.get(C.DATA_INSTALL_OPER, ok_404=True) or {}
            except RestconfError:
                self.logger.info(
                    "Device stopped answering — reload appears to have started.",
                    extra=log,
                )
                return
            status, detail = _classify_ops(_find_op_records(data, op_uuid))
            last_detail = detail
            tokens = _version_state_tokens(data, version_str)
            last_tokens = tokens
            if status == "failure":
                raise UpgradeAbort(
                    f"install activate FAILED per the device's operation ledger: "
                    f"{detail} — the device did not reload; check 'show install "
                    "log'. The device is otherwise unchanged."
                )
            states = {_classify_state(t) for t in tokens}
            if states & {"activated", "uncommitted", "committed"}:
                self.logger.info("Activation confirmed (state: %s).", sorted(tokens), extra=log)
                return
            if status in ("running", "success"):
                absent_streak = 0
                if not engaged:
                    engaged = True
                    # Evidence-based budget switch: the device's own ledger says
                    # the activation is RUNNING, so the short did-it-register
                    # window no longer applies. Long activations are real —
                    # microcode/ROMMON reprogramming on downgrades has been
                    # field-observed to exceed 10 minutes.
                    deadline = started + C.ACTIVATE_ENGAGED_TIMEOUT
                    self.logger.info(
                        "Activate operation registered in the ledger (%s); "
                        "waiting for the reload (budget extended to %ds — "
                        "microcode reprogramming can take a while)...",
                        detail,
                        C.ACTIVATE_ENGAGED_TIMEOUT,
                        extra=log,
                    )
            else:
                absent_streak += 1
                # Only re-send while there is still time to OBSERVE the result:
                # a resend fired just before the abort deadline could engage
                # after the job walks away claiming nothing happened.
                observable = (
                    time.monotonic() + C.POLL_INTERVAL * C.LEDGER_ABSENT_POLLS + C.RPC_TIMEOUT
                ) < deadline
                if not engaged and absent_streak >= C.LEDGER_ABSENT_POLLS and observable:
                    absent_streak = 0
                    resends += 1
                    self.logger.warning(
                        "The ledger never registered the activate after %d polls "
                        "— the engine dropped the request (field-verified failure "
                        "mode); re-sending the same activate...",
                        C.LEDGER_ABSENT_POLLS,
                        extra=log,
                    )
                    resend()
            polls += 1
            if polls % 4 == 0:  # heartbeat every ~2 minutes
                self.logger.info(
                    "Waiting for activation (ledger: %s / install state: %s, "
                    "elapsed %ds of up to %ds)...",
                    detail,
                    sorted(tokens) or "none",
                    int(time.monotonic() - started),
                    C.ACTIVATE_ENGAGED_TIMEOUT if engaged else C.ACTIVATE_START_TIMEOUT,
                    extra=log,
                )
        if engaged:
            # The ledger POSITIVELY recorded our activate running — this is an
            # in-flight operation that outlived the window (plausible on a large
            # stack), NOT a no-op. The device may still reload at any moment.
            raise UpgradeAbort(
                f"The activate is IN FLIGHT per the operation ledger (last phase: "
                f"{last_detail}) but did not produce a reload within "
                f"{C.ACTIVATE_ENGAGED_TIMEOUT}s. DO NOT re-run or modify the "
                "device until it settles — watch 'show install summary'; the "
                "reload may still occur. For very slow activations (large stacks, "
                "extensive microcode reprogramming), raise "
                "ACTIVATE_ENGAGED_TIMEOUT in constants.py."
            )
        if resends:
            raise UpgradeAbort(
                f"Activation never registered in the operation ledger within "
                f"{C.ACTIVATE_START_TIMEOUT}s despite {resends} re-send(s) "
                f"(install state: {sorted(last_tokens) or 'unknown'}). The device "
                "SHOULD be unchanged, but verify 'show install log' before "
                "re-running — a late-engaging activate would reload it "
                "uncommitted (a re-run of this job commits it)."
            )
        raise UpgradeAbort(
            f"Activation did not start within {C.ACTIVATE_START_TIMEOUT}s "
            f"(ledger: {last_detail or 'no record'}; install state: "
            f"{sorted(last_tokens) or 'unknown'}). See 'show install log' on the "
            "device. The device is UNCHANGED (image added, not activated, no "
            "reload pending); re-run this job after addressing the cause."
        )

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
        # Exact-variant match: booting base 17.15.4 must NOT confirm a 17.15.4d
        # target (or vice versa) — the rebuild letter is part of the identity.
        target = _version_key(target_str)
        started = time.monotonic()
        deadline = started + C.RELOAD_TIMEOUT
        went_down = False
        online = False
        last_seen = None
        consecutive = 0
        polls = 0
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                booted = self._current_version(client)
            except RestconfError:
                booted = None
            if booted is None:
                went_down = True  # observed the reboot (unreachable at least once)
            elif not online:
                online = True
                # The OUTAGE number for maintenance planning: this clock starts
                # when the reload was confirmed to begin (activation confirmed /
                # device dropped), so it includes the full dark window.
                self.logger.info(
                    "Device is back online — unreachable for ~%s from reload start.",
                    _fmt_duration(time.monotonic() - started + C.RELOAD_INITIAL_SLEEP),
                    extra=log,
                )
            # Only accept the target AFTER we've seen the device go down, so a box
            # that never actually reloaded cannot satisfy the confirmation.
            if went_down and _version_key(booted) == target:
                consecutive += 1
                if consecutive >= 2:
                    self.logger.info(
                        "Confirmed booted target version **%s** (stable; reload-to-"
                        "confirmed: ~%s).",
                        booted,
                        _fmt_duration(time.monotonic() - started + C.RELOAD_INITIAL_SLEEP),
                        extra=log,
                    )
                    return
            else:
                consecutive = 0
                if _version_key(booted):
                    last_seen = booted
            polls += 1
            if polls % 4 == 0:  # heartbeat every ~2 minutes
                self.logger.info(
                    "Waiting on reload: elapsed %ds of up to %ds (went down: %s, "
                    "online: %s, last version seen: %s).",
                    int(time.monotonic() - started),
                    C.RELOAD_TIMEOUT,
                    went_down,
                    online,
                    last_seen or "none",
                    extra=log,
                )
            time.sleep(C.POLL_INTERVAL)
        if not went_down:
            raise UpgradeAbort(
                f"Device never went down within {C.RELOAD_TIMEOUT}s — the "
                "activation/reload appears not to have happened despite confirmation "
                f"(still answering, last version seen: {last_seen or 'unknown'}). NOT "
                "committed; the device should still be running the previous version. "
                "Check 'show install log' and 'show install summary'."
            )
        raise UpgradeAbort(
            f"Device did not stably boot the target version within {C.RELOAD_TIMEOUT}s "
            f"(online={online}, last definite version: {last_seen or 'unknown'}). "
            "NOT committing — the auto-rollback timer should revert it; verify and "
            "roll back manually if needed."
        )

    def _log_rollback_state(self, client, log):
        """Report the auto-abort (rollback) timer status before committing.

        The oper-state/auto-abort-timer container carries 'state'
        (install-timer-state-active) and 'end-time' leaves (verified against the
        17.15 YANG). This is informational: the commit follows within seconds, so
        an absent timer is not alarming — it only matters if that commit fails,
        and the commit-failure path carries its own manual-rollback guidance.
        """
        data = client.get(C.DATA_INSTALL_OPER, ok_404=True) or {}
        timers = _find_timer_entries(data)
        # Exact suffix match: 'install-timer-state-inactive' CONTAINS 'active',
        # so a substring test would misreport an idle timer as armed.
        active = [
            t for t in timers if str(t.get("state", "")).lower().rsplit("state-", 1)[-1] == "active"
        ]
        if active:
            end = active[0].get("end-time", "unknown")
            self.logger.info(
                "Auto-rollback timer is armed (ends: %s); committing now.", end, extra=log
            )
        elif timers:
            self.logger.info(
                "No auto-rollback timer is active (state: %s); committing "
                "immediately — if the commit fails, roll back manually.",
                [str(t.get("state")) for t in timers],
                extra=log,
            )
        else:
            self.logger.info(
                "Auto-rollback timer status not reported by this release; "
                "committing immediately — if the commit fails, roll back manually.",
                extra=log,
            )

    def _install_commit(self, client, version_str, log):
        """POST install-commit and confirm it via the operation ledger.

        Returns True when positively confirmed (ledger success AND the version's
        committed state cross-checks), False when confirmation is still pending
        (the caller reports it for manual verification). A ledger-recorded
        failure or a refusal body raises.
        """
        self._wait_for_engine_idle(client, log, "install commit")
        self.logger.info("install commit (making the new image permanent)...", extra=log)
        op_uuid = str(uuid_lib.uuid4())
        payload = {"Cisco-IOS-XE-install-rpc:input": {"uuid": op_uuid}}
        response = client.post_rpc(C.OP_COMMIT, payload, timeout=C.RPC_TIMEOUT)
        error_text = _rpc_error_text(response)
        if error_text:
            raise UpgradeAbort(f"install commit was rejected by the install engine: {error_text}")
        outcome = self._await_op(client, op_uuid, C.COMMIT_CONFIRM_TIMEOUT, log, "install commit")
        if outcome == "absent":
            self.logger.warning(
                "The operation ledger never listed the commit (uuid %s); falling "
                "back to install-state confirmation.",
                op_uuid,
                extra=log,
            )
        # Cross-check the version's own committed state in all cases: on ledger
        # success it confirms within a poll; on absent/timeout it IS the check.
        return self._verify_committed(client, version_str, log)

    def _verify_committed(self, client, version_str, log):
        """Poll until install-oper reports the target committed. Returns bool.

        The commit RPC returns before the engine finishes committing (a real
        17.15.4 reported provisioned-uncommitted for a few seconds after the
        RPC), so a single immediate read false-warns. Poll instead; warn only if
        it never confirms within COMMIT_CONFIRM_TIMEOUT.
        """
        started = time.monotonic()
        deadline = started + C.COMMIT_CONFIRM_TIMEOUT
        tokens = []
        while time.monotonic() < deadline:
            self._check_stop()
            tokens = self._state_tokens(client, version_str)
            if _is_committed(tokens):
                self.logger.info(
                    "Commit confirmed via install-oper (state: %s, %ds).",
                    sorted(tokens),
                    int(time.monotonic() - started),
                    extra=log,
                )
                return True
            time.sleep(C.POLL_INTERVAL)
        self.logger.warning(
            "Could not confirm committed state for %s within %ss (state: %s); "
            "verify with 'show install summary'.",
            version_str,
            C.COMMIT_CONFIRM_TIMEOUT,
            sorted(tokens) or "unknown",
            extra=log,
        )
        return False

    def _remove_inactive(self, client, log, fatal=False):
        """Run 'install remove inactive' (idle-gated, ledger-tracked).

        fatal=False (post-commit cleanup): every failure is a warning — the
        upgrade already succeeded. fatal=True (operator-requested pre-upgrade
        clean): failures ABORT — an engineer who asked for a clean that failed
        should investigate, not proceed onto a dirty device.
        """
        self.logger.info("install remove inactive...", extra=log)
        op_uuid = str(uuid_lib.uuid4())
        # INVARIANT (26.1.1+): 'inactive' sits inside the now-mandatory choice
        # remove-type-by-choice (version | path | inactive | name) — a uuid-only
        # remove is rejected. Always send exactly one of the choice members.
        payload = {"Cisco-IOS-XE-install-rpc:input": {"uuid": op_uuid, "inactive": True}}
        try:
            self._wait_for_engine_idle(client, log, "install remove inactive")
            response = client.post_rpc(C.OP_REMOVE, payload, timeout=C.RPC_TIMEOUT)
            error_text = _rpc_error_text(response)
            if error_text:
                if fatal:
                    raise UpgradeAbort(
                        f"The requested clean was refused by the install engine: "
                        f"{error_text} — device unchanged; investigate before "
                        "re-running."
                    )
                self.logger.warning(
                    "remove inactive was refused (non-fatal): %s", error_text, extra=log
                )
                return
            outcome = self._await_op(
                client, op_uuid, C.COMMIT_CONFIRM_TIMEOUT, log, "install remove inactive"
            )
        except (RestconfError, UpgradeAbort) as exc:
            if fatal:
                raise
            self.logger.warning("remove inactive failed (non-fatal): %s", exc, extra=log)
            return
        if outcome == "success":
            self.logger.info("Inactive images removed (ledger-confirmed).", extra=log)
        elif fatal:
            raise UpgradeAbort(
                f"The requested clean was not ledger-confirmed ({outcome}); "
                "verify 'show install summary' before re-running."
            )
        else:
            self.logger.warning(
                "remove inactive issued but not ledger-confirmed (%s); verify "
                "with 'show install summary'.",
                outcome,
                extra=log,
            )

    def _clean_device(self, client, target_str, log):
        """Operator-requested pre-upgrade clean — the deliberate override.

        Logs what the install DB tracks before and after, then lets the ENGINE
        decide what is removable ('install remove inactive' clears inactive and
        staged versions plus unreferenced .bin/.pkg files — never the running
        committed image). State over inference: we report observed outcomes,
        not guessed file lists.
        """
        entries, _staged = self._inventory_other_versions(client, target_str)
        if not entries:
            self.logger.info(
                "Clean requested: the install DB tracks nothing besides the "
                "running image — nothing to remove.",
                extra=log,
            )
            return
        self.logger.warning(
            "CLEAN REQUESTED — removing all software this device is not "
            "running. Install DB currently also tracks: %s. This includes any "
            "version staged by another engineer and the previous soak/rollback "
            "image (rollback afterward = re-run this job targeting the old "
            "version).",
            "; ".join(entries),
            extra=log,
        )
        self._remove_inactive(client, log, fatal=True)
        after, _ = self._inventory_other_versions(client, target_str)
        self.logger.info(
            "Clean complete. Install DB now tracks besides the running image: %s.",
            "; ".join(after) or "nothing",
            extra=log,
        )

    def _sync_nautobot(self, device, target_version, log):
        with transaction.atomic():
            device.software_version = target_version
            device.validated_save()
        self.logger.info(
            "Updated Nautobot Device.software_version to %s.", target_version, extra=log
        )


# --------------------------------------------------------- module utilities --


def _progress_label(size, expected):
    """Human progress string for the copy watcher."""
    mb = 1_000_000
    if size is None:
        return "no size data"
    label = f"{size // mb} MB"
    if expected:
        pct = min(100, round(size * 100 / expected))
        return f"{label} / {expected // mb} MB ({pct}%)"
    return f"{label} (expected size unknown)"


def _fetch_hints(url):
    """Actionable causes for a device failing to download from ``url``.

    The dominant real-world cause for an https source is TLS: the firmware
    server's certificate is not trusted by the device (a browser downloading the
    same URL proves nothing — different trust store).
    """
    hints = []
    if url.lower().startswith("https://"):
        hints.append(
            "MOST LIKELY: the device does not trust the firmware server's TLS "
            "certificate (self-signed). Fix: use the HTTP URL on the "
            "mgmt-restricted network (edit the image's download_url to "
            "http://<host>:9080/images/...), or install the server's CA in a "
            "device trustpoint (crypto pki trustpoint + authenticate)"
        )
    hints.append(
        "check the device can actually reach the URL host (VRF/source-interface: "
        "'ip http client source-interface ...'), and test from the device CLI: "
        "copy " + url + " null:"
    )
    return f"Likely causes: {'; '.join(hints)}."


def _interpret_copy_failure(exc, url):
    """Turn a copy-RPC failure into an actionable message.

    The device reports fetch failures as an opaque '%Error opening ... (I/O
    error)' inside an HTTP 400.
    """
    text = str(exc)
    lowered = text.lower()
    if "error opening" in lowered or "i/o error" in lowered:
        return (
            f"Image copy failed — device could not fetch {url}. "
            f"{_fetch_hints(url)} Device said: {text}"
        )
    return f"Image copy failed — the device rejected the copy request: {text}"


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


def _mode_suffix(value):
    """Normalize a boot-mode enum to its suffix (install / bundle / ...).

    Handles BOTH enum families: install-boot-mode-* (the oper-state boot-mode
    leaf's typedef on 17.15.x) and install-mode-* (the older/rollback typedef).
    The longer prefix is stripped first; an unprefixed value passes through as-is.
    """
    text = str(value).strip().lower()
    for prefix in ("install-boot-mode-", "install-mode-"):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def _boot_mode_suffixes(data):
    """Normalized boot-mode suffixes from install-oper data.

    Prefers boot-mode values found inside 'oper-state' containers (the live
    per-location state on 17.15.x) so historical rollback-point entries are not
    read as the current mode; falls back to a global key search when no
    oper-state carries one.
    """
    scoped = []
    _collect_oper_state_modes(data, scoped)
    values = scoped or [value for key in C.BOOT_MODE_KEYS for value in _find_all_values(data, key)]
    return [_mode_suffix(value) for value in values]


def _collect_oper_state_modes(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "oper-state" and isinstance(value, dict):
                for mode_key in C.BOOT_MODE_KEYS:
                    out.extend(_find_all_values(value, mode_key))
            else:
                _collect_oper_state_modes(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_oper_state_modes(item, out)


def _find_version_rows(data):
    """Every install-version-state-info row anywhere in install-oper data.

    This is the DOCUMENTED list for version state (key 'version-state', leaves
    'version-state' + 'version' — YANG-verified structurally identical across
    every supported release, 17.9.1 through 26.1.1, and field-verified on real
    17.15.x where its 'version' leaf carries the engine's own full internal
    identifier, e.g. '17.15.05.0.8370'). Scoping to this list — instead of
    matching any 'version'-named key anywhere — excludes the module's OTHER
    version-bearing leaves (rollback labels, txn echoes) whose junk composites
    ('17.15.04.0.6839.<epoch>..IOSXE') previously had to be filtered out by
    pattern heuristics.
    """
    rows = []
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "install-version-state-info" and isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))
            else:
                rows.extend(_find_version_rows(value))
    elif isinstance(data, list):
        for item in data:
            rows.extend(_find_version_rows(item))
    return rows


def _version_state_tokens(data, version_str):
    """All version-state tokens (lowercased) for rows matching version_str.

    Exact-variant matching via _version_key: on a device where 17.15.4 and
    17.15.4d coexist (guaranteed mid-rebuild-upgrade), only the requested
    variant's rows count — the other variant's committed/pending rows must
    never satisfy this variant's gates.
    """
    target = _version_key(version_str)
    if target is None:
        return []
    return [
        str(row.get("version-state", "")).strip().lower()
        for row in _find_version_rows(data)
        if str(row.get("version-state", "")).strip() and _version_key(row.get("version")) == target
    ]


def _classify_state(token):
    """Map a version-state value to the job's state classes.

    The leaf is the CLOSED typedef install-version-state (RESTCONF encodes
    enums by name; family verified across 17.9.1-26.1.1): in-progress =
    "marked for activation", the NORMAL resting state of an added image
    (real-device verified); provisioned-{committed,uncommitted} = commit
    state; installed = added; present = files on disk WITHOUT install-DB
    staging (must never satisfy staged gates — real-device verified);
    invalid (mid-extraction) and unknown (17.18.1+) and anything
    unrecognized classify as 'other', which never satisfies any gate —
    unclassifiable states fail safe.
    """
    suffix = token.strip().lower().rsplit("state-", 1)[-1]
    return {
        "provisioned-committed": "committed",
        "provisioned-uncommitted": "uncommitted",
        "in-progress": "pending",
        "installed": "added",
        "present": "present",
    }.get(suffix, "other")


def _is_committed(tokens):
    """True only if a committed state is present and no pending state is.

    'Pending' = activated, uncommitted, or an open install transaction (the
    'pending' class, e.g. install-version-state-in-progress = marked for
    activation). Aggregating across rows is conservative: a stale pending row
    makes this return False, so the caller commits to be safe (a harmless no-op
    if already committed) rather than risk skipping a real commit.
    """
    classes = [_classify_state(t) for t in tokens]
    return "committed" in classes and not any(
        c in ("activated", "uncommitted", "pending") for c in classes
    )


def _full_version_string(data, version_str):
    """The device's full internal version for ``version_str`` (or None).

    Read directly from the documented install-version-state-info rows, whose
    'version' leaf carries the identifier the install DB indexes by (e.g.
    '17.15.05.0.8370', '17.15.04d.0.6839' for rebuilds) — the exact form the
    activate RPC requires for re-activations (bare strings hang the engine;
    field-verified). Exact-variant matching; the shortest matching value wins
    as a cheap defense should a release ever duplicate rows.
    """
    target = _version_key(version_str)
    if target is None:
        return None
    best = None
    for row in _find_version_rows(data):
        value = str(row.get("version", "")).strip()
        if value and _version_key(value) == target:
            if best is None or len(value) < len(best):
                best = value
    return best


def _rpc_error_text(response):
    """Failure text from an install RPC response body, or "" if it looks clean.

    Install RPCs return HTTP 2xx even when the engine refuses the operation
    (verified repeatedly on a real 17.15.4); the refusal text rides in the
    response body. Bodies are short acks (uuid/output), so marker scanning is
    safe here — unlike the big oper blobs, where substring matching burned us.
    """
    if not response or not isinstance(response, dict):
        return ""
    text = str(response)
    lowered = text.lower()
    markers = ("fail", "error", "not allowed", "cannot", "reject", "invalid")
    if any(marker in lowered for marker in markers):
        return text
    return ""


def _find_inventory_entries(data):
    """Collect device-inventory entries (dicts carrying an 'hw-type' key)."""
    found = []
    if isinstance(data, dict):
        if "hw-type" in data:
            found.append(data)
        else:
            for value in data.values():
                found.extend(_find_inventory_entries(value))
    elif isinstance(data, list):
        for item in data:
            found.extend(_find_inventory_entries(item))
    return found


def _find_op_records(data, op_uuid):
    """Every install operation record (running or history) for ``op_uuid``.

    The install engine keeps a LEDGER of operations keyed by the RPC-supplied
    uuid: install-oper-data/install-oper (under execution) and install-oper-hist
    (completed) — verified populated on a real 17.15.x. One RPC can yield
    MULTIPLE records (a real add produced op-id 1 'download' + op-id 2 'add'),
    so completion means EVERY record for the uuid is complete.
    """
    found = []
    if isinstance(data, dict):
        if str(data.get("op-uuid", "")).strip().lower() == str(op_uuid).strip().lower():
            found.append(data)
        else:
            for value in data.values():
                found.extend(_find_op_records(value, op_uuid))
    elif isinstance(data, list):
        for item in data:
            found.extend(_find_op_records(item, op_uuid))
    return found


def _classify_ops(records):
    """Reduce a uuid's ledger records to (status, detail).

    status: 'absent' | 'running' | 'success' | 'failure'. detail names the
    failing/current phase (from the per-transaction txn-cmd/txn-status rows,
    e.g. 'install-txn-add-postchk') so aborts and heartbeats quote the engine's
    own account instead of an inference. Field-verified value shapes: op-done
    'op-complete'/'op-not-complete' (suffix test would confuse them — check for
    'not' explicitly), op-status 'install-op-succ', txn-status
    'install-txn-sts-succ'/'-fail'/'-dep-fail'/'-timeout'/'-cancel'/...
    """
    if not records:
        return ("absent", "no ledger record for this operation uuid")
    failures = []
    running_phase = ""
    all_done = True
    all_succ = True
    for record in records:
        done_token = str(record.get("op-done", "")).strip().lower()
        status_token = str(record.get("op-status", "")).strip().lower()
        if "revert" in done_token:
            # 17.18.1+/26.1.1: op-done gains 'op-reverted' — the engine ran the
            # operation and then AUTO-REVERTED it after detecting failures. The
            # model's when-clauses hide the txn rows for reverted ops, so this
            # token IS the whole story: a terminal failure — never 'running'
            # (which would poll to timeout) and never 'absent' (which would
            # re-send an activate the engine just rolled back).
            failures.append(
                f"operation reverted by the engine (op-done: {done_token}"
                + (f", op-status: {status_token}" if status_token else "")
                + ")"
            )
            continue
        done = "complete" in done_token and "not" not in done_token
        txns = []
        for key, value in record.items():
            if str(key).startswith("install-txn-sum") and isinstance(value, list):
                txns.extend(t for t in value if isinstance(t, dict))
        failed_txns = [
            t
            for t in txns
            if any(
                marker in str(t.get("txn-status", "")).lower()
                for marker in ("fail", "timeout", "cancel", "disconnect")
            )
        ]
        if failed_txns or any(m in status_token for m in ("fail", "timeout", "cancel", "abort")):
            phases = [
                f"{t.get('txn-cmd', '?')} -> {t.get('txn-status', '?')}" for t in failed_txns
            ] or [status_token or "unknown"]
            sub_states = [
                entry.get("txn-sub-state")
                for t in failed_txns
                for entry in (t.get("install-txn-sub-sts-log") or [])
                if isinstance(entry, dict) and entry.get("txn-sub-state")
            ]
            suffix = f" (sub-states: {sub_states})" if sub_states else ""
            failures.append("; ".join(phases) + suffix)
            continue
        if not done:
            all_done = False
            if txns:
                running_phase = str(txns[-1].get("txn-cmd", "")) or running_phase
        if "succ" not in status_token:
            all_succ = False
    if failures:
        return ("failure", "; ".join(failures))
    if all_done and all_succ:
        return ("success", f"{len(records)} ledger record(s) complete")
    return ("running", running_phase or "phase not yet reported")


def _find_timer_entries(data):
    """Collect auto-abort-timer containers ({'state':…, 'end-time':…}) anywhere.

    The 17.15 YANG puts them at install-location-information[]/oper-state/
    auto-abort-timer with leaves 'state' (install-timer-state-*) and 'end-time'.
    """
    found = []
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "auto-abort-timer" and isinstance(value, dict):
                found.append(value)
            else:
                found.extend(_find_timer_entries(value))
    elif isinstance(data, list):
        for item in data:
            found.extend(_find_timer_entries(item))
    return found


def _uq(value):
    """Percent-encode a RESTCONF list-key value (full paths contain '/')."""
    return urllib_parse.quote(str(value), safe="")


def _find_qfs_entries(data):
    """q-filesystem entry dicts (carrying 'partitions') in any wrapper shape."""
    found = []
    if isinstance(data, dict):
        if isinstance(data.get("partitions"), list):
            found.append(data)
        else:
            for value in data.values():
                found.extend(_find_qfs_entries(value))
    elif isinstance(data, list):
        for item in data:
            found.extend(_find_qfs_entries(item))
    return found


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


def _flash_frees(data, fs_names):
    """(name, free-bytes) for EVERY matching flash partition, stack members too.

    Matches a partition whose name equals a configured name OR is that name with
    a stack-member suffix ('flash-1', 'flash:1') — but never 'bootflash'/
    'usbflash'. On a stack, one entry per member is returned; 'install add'
    distributes packages to every member, so all of them matter.
    """
    out = []
    for partition in _find_partitions(data):
        name = str(partition.get("name", "")).strip().rstrip(":").lower()
        for fs in fs_names:
            if name == fs or name.startswith(fs + "-") or name.startswith(fs + ":"):
                free = _partition_free(partition)
                if free is not None:
                    out.append((name, free))
                break
    return out


def _find_file_context(data, image_file_name):
    """(entry_keys, partition_name, file_entry) for an OBSERVED file — or None.

    Walks a full q-filesystem document carrying the enclosing q-filesystem
    entry keys and partition name, so a sighting yields the complete keyed
    address (correct member on stacks, correct partition, real full-path) —
    observation, not hypothesis.
    """

    def _walk(node, keys, pname):
        if isinstance(node, dict):
            if all(k in node for k in ("fru", "slot", "bay", "chassis")):
                keys = tuple(str(node[k]) for k in ("fru", "slot", "bay", "chassis"))
            if "name" in node and isinstance(node.get("partition-content"), list):
                pname = str(node.get("name"))
            path = node.get("full-path") or node.get("name") or node.get("filename")
            if path:
                basename = str(path).split(":")[-1].rsplit("/", 1)[-1]
                if basename == image_file_name and (
                    node.get("full-path") or node.get("file-size") or node.get("size")
                ):
                    return keys, pname, node
            for value in node.values():
                found = _walk(value, keys, pname)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _walk(item, keys, pname)
                if found is not None:
                    return found
        return None

    return _walk(data, None, None)


def _file_size_bytes(data, image_file_name):
    """Find a named file in q-filesystem data and return its size in bytes.

    File sizes (image-files/file-size, partition-content/size) are reported in
    BYTES on supported releases (>= 17.9 per the model's 2022-07-01 revision;
    only partition total-size/used-size are kilobytes). Matches the file's
    basename for EQUALITY (not substring) so a longer filename that merely
    contains the target name can't mask a truncated copy.
    """
    if isinstance(data, dict):
        path = data.get("full-path") or data.get("name") or data.get("filename")
        if path:
            basename = str(path).split(":")[-1].rsplit("/", 1)[-1]
            if basename == image_file_name:
                size = data.get("file-size") or data.get("size")
                try:
                    return int(size)
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
