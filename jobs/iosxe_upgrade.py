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
  3. Transfer + integrity: async xcopy by default (an install-engine
     operation tracked by its uuid in the ledger; success = the engine's
     published verdict confirmed byte-exact from the package inventory, a
     keyed read, or the authoritative listing), with the classic copy RPC
     as the fallback tier (worker thread, file-size progress polling,
     size verification on completion)
     (Run scope 'stage-copy' STOPS here — staged, nothing armed)
  4. install add -> tracked to COMPLETION in the engine's operation ledger
     (install-oper / install-oper-hist records keyed by our RPC uuid; install
     state inference only as a fallback; Run scope 'stage-add' STOPS here) ->
     engine-idle gate (sys-activity) -> install activate (non-ISSU, by full
     internal version; re-sent on ledger-absent evidence; ledger-ENGAGED runs
     get an extended budget for microcode reprogramming; rollback timer
     checked after reload) -> reload
  5. Poll until the target version actually booted AND every pre-upgrade
     chassis rejoined (stack members / the standalone chassis) -> install
     commit (ledger-tracked)
  6. Post-checks + sync Nautobot's Device.software_version
  7. Optional: install remove inactive (off by default)

NOTE: The core flow is hardware-validated (Catalyst 9300 single switches and
a 2-member stack; trains 17.12 -> 17.15 <-> 17.18 <-> 26.1; lettered rebuilds;
serial batches; from Nautobot 3.1 and 2.4). The project remains under active
development - new capabilities carry their validation state in the README -
and every run should start with Dry-run.
"""

from __future__ import annotations

import threading
import time
import uuid as uuid_lib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from celery.exceptions import SoftTimeLimitExceeded

try:  # Celery task-context propagation into worker threads (see run()).
    from celery import current_task
    from celery.app import pop_current_task, push_current_task
except ImportError:  # pragma: no cover - non-Celery environments (tests)
    current_task = None
    pop_current_task = push_current_task = None
from django.db import close_old_connections
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
    SoftwareVersion,
)
from nautobot.extras.models import (
    DynamicGroup,
    Role,
    SecretsGroup,
    Status,
    Tag,
)

from . import constants as C
from .install_engine import (
    EngineBusy,
    InstallEngineMixin,
    JobStopped,
    LedgerOpFailure,
    UpgradeAbort,
    XcopyFailed,
    _auth_hint,
    _classify_health,
    _env_ok,
    _find_inventory_entries,
    _fmt_duration,
    _health_is_clean,
    _health_regressions,
    _is_committed,
    _parse_cdp,
    _parse_env,
    _parse_interfaces,
    _parse_lldp,
    _parse_trunks,
    _redact_url,
    _version_key,
    _xcopy_url_unsupported,
)
from .restconf import RestconfClient, RestconfError

name = "IOS-XE Upgrades"

# The lookbehind rejects candidates glued to an alphanumeric: real image
# names like 'c8000v-universalk9.17.15.04a.SPA.bin' would otherwise match
# '9.17.15' LEFTMOST (the platform token's trailing 'k9' donates its digit)
# and misparse every standard C8000V/9800 filename (review finding).


class IOSXEUpgrade(InstallEngineMixin, Job):
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
        required=False,
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
            "then select the specific devices to act on (or select all). Optional "
            "when Dynamic groups supply the roster; the final roster is the "
            "UNION of both, deduplicated."
        ),
    )
    dynamic_groups = MultiObjectVar(
        model=DynamicGroup,
        required=False,
        query_params={"content_type": "dcim.device"},
        label="Dynamic groups",
        description=(
            "Device Dynamic Groups to upgrade — resolved LIVE at run start "
            "via the platform's own membership computation (filter-based, "
            "set-based 'group of groups', and static groups all supported; "
            "never a stale cache). The run log records each group's "
            "resolution (exact count, method, first 20 names) — run Dry-run "
            "first to preview it. The final roster is the union with any "
            "explicitly selected devices; an empty total refuses."
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
        label="Clean device first",
        default=False,
        description=(
            "Before upgrading, remove ALL software this device is not "
            "running: inactive packages and leftover image files. See "
            "'Cleaning a device first' in the project README."
        ),
    )
    save_config = BooleanVar(
        label="Save running-config before reload",
        default=False,
        description=(
            "Write running-config to startup-config before activation "
            "reload. Only effective for Full runs. See 'Saving "
            "running-config before the reload' in the project README."
        ),
    )
    save_config_after = BooleanVar(
        label="Save running-config after commit",
        default=False,
        description=(
            "Write running-config to startup-config after the upgrade is "
            "committed. Only effective for Full runs. See 'Saving "
            "running-config after the commit' in the project README."
        ),
    )
    gc_backup = BooleanVar(
        label="Golden Config backup (before & after)",
        default=False,
        description=(
            "Run the Golden Config backup job for the selected devices before "
            "any upgrades start and again after all devices finish. Requires "
            "the Golden Config app. See 'Golden Config backups' in the "
            "project README."
        ),
    )
    health_checks = BooleanVar(
        label="Pre/post health checks",
        default=False,
        description=(
            "Snapshot ports, CDP/LLDP neighbors, and environment sensors "
            "before activation and compare after commit (report-only, "
            "convergence-aware). Only effective for Full runs. See 'Pre/post "
            "health checks' in the project README."
        ),
    )
    suppress_avc_noise = BooleanVar(
        label="Quiet SELinux log noise on terminals",
        default=False,
        description=(
            "Quiets SELinux chatter on the physical console and SSH "
            "terminal-monitor sessions. No effect when Dry-run is selected. "
            "See 'SELinux AVC log events' in the project README."
        ),
    )
    transfer_method = ChoiceVar(
        choices=(
            ("xcopy", "Async xcopy (default - classic-copy fallback)"),
            ("copy", "Classic copy only"),
        ),
        default="xcopy",
        required=False,
        label="Image transfer method",
        description=(
            "How the image reaches the device. Async xcopy (default) runs "
            "the transfer inside the install engine — uuid-keyed ledger "
            "tracking, immune to the device's ~10-minute limit on the "
            "blocking copy RPC — and FALLS BACK to classic copy when a "
            "pre-fire guard (ported image URL, no recorded file size) or a "
            "device-reported terminal failure says xcopy cannot work here. "
            "Classic copy only skips xcopy entirely (the pre-2.0 behavior). "
            "See 'Image transfer methods' in the project README."
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
        # one worst-case device (transfer + add 1200 + reload 120+1800 + slack),
        # plus up to 2x GC_BACKUP_TIMEOUT (900s each) of serial Golden Config
        # backup waits and up to ~HEALTH_CONVERGENCE_TIMEOUT + one capture
        # (~13 min worst case) of post-upgrade health polling, when those
        # options are on. The DEFAULT transfer (async xcopy) can hold the
        # watch up to WAN_TRANSFER_TIMEOUT_MIN (90 min) + 300s slack; a
        # terminal xcopy failure then STACKS the classic-copy fallback
        # (COPY_TIMEOUT 3600s) on top — a worst-case both-tiers device can
        # exceed the soft limit and be cooperatively stopped mid-fallback
        # (recovered by an idempotent re-run). Raise the constant and these
        # limits TOGETHER for very slow WANs.
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
            "dynamic_groups",
            "target_version",
            "run_scope",
            "clean_before",
            "transfer_method",
            "save_config",
            "save_config_after",
            "gc_backup",
            "health_checks",
            "suppress_avc_noise",
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
        # New-since-1.0 inputs carry DEFAULTS: a ScheduledJob stores the
        # kwargs its form had at save time, and Nautobot fires stored kwargs
        # verbatim — a missing key with no default is a TypeError before the
        # job's first log line (review-verified against 2.4.36).
        dynamic_groups=None,
        target_version,
        run_scope,
        clean_before,
        save_config,
        save_config_after,
        gc_backup,
        health_checks,
        suppress_avc_noise,
        # Same ScheduledJob-compat rule: added post-1.0 (PR #90), so a
        # pre-#90 stored schedule lacks the key — default to the advertised
        # default (the stale-'install' guard still vets stored VALUES).
        transfer_method="xcopy",
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
        # Resolve the roster FIRST (dynamic groups resolve live, with their
        # rosters logged) so the start line reports the true total.
        device_list = self._resolve_roster(devices, dynamic_groups)
        # Device names are NOT globally unique (only per location+tenant),
        # and group selection makes same-named rosters reachable — outcome
        # accounting therefore keys on a per-run unique LABEL, never the raw
        # name (a collision would silently merge two devices' outcomes).
        name_counts = Counter(d.name for d in device_list)
        labels = {
            d.pk: (
                str(d.name)
                if d.name and name_counts[d.name] == 1
                else f"{d.name or 'unnamed'} ({d.location} / {d.pk})"
            )
            for d in device_list
        }
        self.logger.info(
            "Starting IOS-XE upgrade to **%s** for %d selected device(s)%s "
            "— nautobot-upgrades v%s.",
            target_version,
            len(device_list),
            " (DRY-RUN)" if dryrun else "",
            C.JOB_VERSION,
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
            self.logger.info(
                "Filters applied (they scope the device PICKER only — never group membership): %s.",
                filter_summary,
            )

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
                    save_config_after,
                    suppress_avc_noise,
                    health_checks,
                    transfer_method,
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

        # Opt-in Golden Config safety net: snapshot configs BEFORE anything
        # reloads. Fail-closed — a requested safety net that can't run stops
        # the run before any device is touched. Dry-run stays read-only.
        if gc_backup:
            if dryrun:
                self.logger.info(
                    "DRY-RUN: would run the Golden Config backup for %d device(s) "
                    "before the upgrades and again after they finish.",
                    len(device_list),
                )
            else:
                self._run_gc_backup(device_list, "before")

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
                results[labels[device.pk]] = summary
                if device_failed:
                    failed.append(labels[device.pk])
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
                    never_started.append(labels[device.pk])
                elif future.done() and labels[device.pk] not in results:
                    # Completed while the signal was in flight — never drop a
                    # finished device's outcome.
                    _, summary, device_failed = future.result()
                    results[labels[device.pk]] = summary
                    if device_failed:
                        failed.append(labels[device.pk])
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
        # Opt-in Golden Config snapshot of the post-upgrade configs. Runs even
        # when some devices failed (capturing state then is exactly the point)
        # but NOT on the cooperative-stop path (the post-mortem raises above).
        # Warn-only: a failed after-backup must not un-succeed completed
        # upgrades — it is named here and in the job log instead.
        if gc_backup and not dryrun:
            try:
                self._run_gc_backup(device_list, "after")
            except SoftTimeLimitExceeded:
                raise  # operator cancel / time budget is never swallowed
            except Exception as exc:  # noqa: BLE001 - warn-only by design: nothing
                # that happens in the after-backup may un-succeed completed upgrades
                self.logger.warning(
                    "Golden Config backup (after) failed: %s — completed upgrades are unaffected.",
                    exc,
                )

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
        save_config_after=False,
        suppress_avc_noise=False,
        health_checks=False,
        transfer_method="copy",
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
            return self._handle_already_on_target(
                client, device, target_version, dryrun, log, run_scope=run_scope
            )

        # Opt-in log-only write, applied as early as possible for runs that
        # will actually DO something — after the already-on-target no-op
        # branch (a lasting config write for zero benefit there — review
        # finding), before the gates whose partitions read is the first
        # filesystem walk. Never in dry-run.
        if suppress_avc_noise:
            if dryrun:
                self.logger.info(
                    "DRY-RUN: would insert the SELinux AVC suppression filter "
                    "(logging discriminator %s, console + terminal-monitor) "
                    "into running-config.",
                    C.AVC_DISCRIMINATOR_NAME,
                    extra=log,
                )
            else:
                self._apply_avc_suppression(client, log)

        # -- 2. Pre-flight gates ---------------------------------------------
        self._gate_version_floor(current, log)
        self._gate_install_mode(client, log)

        image = self._resolve_image(device, target_version, log)
        # Saved automations (ScheduledJobs) bypass the form's choice
        # validation, so a value from a removed option (the 2026-07
        # engine-download 'install') can arrive here — fail LOUDLY instead
        # of silently rerouting a stored intent.
        if transfer_method in (None, ""):
            transfer_method = "xcopy"  # optional-field omission -> the default
        elif transfer_method not in ("xcopy", "copy"):
            raise UpgradeAbort(
                f"Unknown transfer method {transfer_method!r}. The 'install' "
                "(engine download) method was removed 2026-07 — re-save the "
                "scheduled job/automation with 'xcopy' (default, classic-copy "
                "fallback) or 'copy'."
            )
        # Pre-fire fallback guards (dry-run visible): when a wire-proven
        # precondition says xcopy cannot work for THIS image, the run falls
        # back to the classic copy tier up front — a logged decision, not an
        # abort (the operator picked the default; the job picks the tier).
        xcopy_demoted = False
        if transfer_method == "xcopy":
            fallback_reason = None
            if not image.image_file_size:
                fallback_reason = (
                    "the image has no recorded file size (xcopy's byte-exact "
                    "confirmation needs it — record it on the "
                    "SoftwareImageFile to use xcopy)"
                )
            else:
                url_problem = _xcopy_url_unsupported(image.download_url)
                if url_problem:
                    fallback_reason = (
                        f"the image URL carries {url_problem} — bench- and "
                        "wire-proven (17.18.03) to fail inside the device's "
                        "express-copy parser before a single packet is sent; "
                        "serve the image on a standard port with no port in "
                        "the URL to use xcopy"
                    )
            if fallback_reason:
                transfer_method = "copy"
                xcopy_demoted = True
                self.logger.warning(
                    "Async xcopy is unavailable for this image: %s. This run "
                    "uses the classic copy tier instead.",
                    fallback_reason,
                    extra=log,
                )
        # Operator-requested pre-upgrade clean (deliberate override of the
        # staged-conflict stop) — never in dry-run (it writes). Runs BEFORE the
        # free-space gate so the gate evaluates the CLEANED flash.
        if clean_before and not dryrun:
            self._clean_device(client, target_str, log)

        # Discover the writable filesystem from the device itself (flash: on
        # Catalyst switches, bootflash: on C8000V) — every downstream step
        # (space gate, copy destination, install add path) uses this value.
        # Per-device local, never instance state: device threads run in parallel.
        target_fs, partitions = self._discover_target_fs(client, log)
        self._gate_free_space(client, image, log, target_fs, partitions)

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
                    f"would PRE-STAGE (copy only): '{_redact_url(image.download_url)}' to "
                    f"{target_fs}{image.image_file_name} — no install activity"
                )
            elif run_scope == "stage-add":
                planned = (
                    f"would PRE-STAGE (copy + install add) {target_str} — no activate, no reload"
                )
            else:
                planned = (
                    f"would copy '{_redact_url(image.download_url)}' to "
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
                # Platform fact, not a device claim: RPC-triggered reloads
                # never prompt to save (there is no non-SNMP source for the
                # saved/unsaved determination, and the SNMP bridge is not a
                # dependency this project accepts).
                cfg_note = (
                    " Would save running-config to startup-config before the reload."
                    if save_config
                    else " Reminder: the reload never prompts to save — unsaved "
                    "running-config changes would be lost (tick 'Save "
                    "running-config before reload' or save manually)."
                )
                if save_config_after:
                    cfg_note += (
                        f" Would save running-config{' again' if save_config else ''} "
                        "AFTER the commit (post-commit save)."
                    )
                if health_checks:
                    cfg_note += (
                        " Would snapshot health (ports/CDP/LLDP/environment) "
                        "before activation and compare after commit."
                    )
            if transfer_method == "xcopy":
                cfg_note += (
                    " Async xcopy: the install engine would fetch the image "
                    "in the background (uuid-keyed ledger tracking, "
                    "byte-exact confirmed; all run scopes, including Step 1); "
                    "classic copy stands by as the fallback tier."
                )
            else:
                cfg_note += (
                    (
                        " Classic copy (xcopy ruled out by a pre-fire guard — "
                        "see the warning above):"
                        if xcopy_demoted
                        else " Classic copy only:"
                    )
                    + " the blocking copy RPC would run in a worker thread, "
                    "watched to a size-verified completion."
                )
            return f"DRY-RUN ok:{clean_note}{cfg_note} {planned}. All pre-flight gates passed."

        # -- 3. Transfer + integrity ------------------------------------------
        # Async xcopy is the DEFAULT tier: the install engine runs the
        # transfer (uuid-keyed ledger record — immune to the DMI/ConfD ~600s
        # blocking-RPC ceiling that killed classic copy on slow WANs, field
        # report 2026-07) and the job confirms from device-published state.
        # Classic copy is the FALLBACK tier: taken up front when a pre-fire
        # guard already decided it (see the fallback guards after
        # _resolve_image), or here when xcopy ends in a POSITIVELY TERMINAL
        # device-reported failure. Ambiguous ends never fall back — the
        # engine may still be writing the file (see XcopyFailed).
        if transfer_method == "xcopy":
            skip, pre_fire_size = self._xcopy_precheck_skip(
                client, image, log, target_fs, partitions
            )
            if not skip:
                try:
                    self._xcopy_image(
                        client,
                        image,
                        log,
                        target_fs,
                        partitions_data=partitions,
                        pre_fire_size=pre_fire_size,
                    )
                except XcopyFailed as exc:
                    self.logger.warning(
                        "xcopy ended in a device-reported terminal failure — "
                        "falling back to classic copy. (%s) NOTE: on a slow "
                        "WAN the classic copy can itself die at the device's "
                        "~600s blocking-RPC ceiling; if that happens, fix the "
                        "xcopy precondition rather than re-running the "
                        "fallback.",
                        exc,
                        extra=log,
                    )
                    self._copy_image(client, image, log, target_fs)
        else:
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
        # Capture the chassis roster first (stack members on switches; the one
        # chassis on a standalone switch or router): after the reload, every
        # entry must rejoin before we commit.
        roster = self._member_roster(client)
        if roster:
            self.logger.info(
                "Chassis roster captured: %d (%s) — %s must return after the reload.",
                len(roster),
                sorted(roster),
                "all stack members" if len(roster) > 1 else "the chassis",
                extra=log,
            )
        # Each write gets its OWN correlation uuid: the engine's operation
        # ledger is keyed by it, so per-operation uuids keep the tracking exact
        # (one shared uuid would make add records vouch for the commit).
        ledger_confirmed_add = self._install_add(
            client,
            image,
            str(uuid_lib.uuid4()),
            log,
            target_fs,
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
        # -- Pre-activation health baseline (opt-in). Captured as late as
        # possible before the disruptive verb so the baseline reflects the
        # device as it enters the reload. FAIL-CLOSED: a requested baseline
        # that cannot be captured aborts BEFORE activation — nothing has
        # reloaded yet, so aborting here is free.
        health_pre = None
        if health_checks:
            try:
                health_pre = self._capture_health_snapshot(client, want_trunks=True)
            except RestconfError as exc:
                raise UpgradeAbort(
                    f"Health checks were requested but the pre-upgrade snapshot "
                    f"failed ({exc}) — aborting before activation (a requested "
                    "baseline must exist). Untick the option to upgrade without "
                    "checks."
                ) from exc
            up_ports = sum(
                1 for s in health_pre["interfaces"].values() if s.get("admin") and s.get("oper")
            )
            self.logger.info(
                "Health baseline: %d port(s) up (%d trunk/infrastructure), "
                "%d CDP neighbor(s), %d LLDP, %d healthy sensor(s); last "
                "reboot: '%s'.",
                up_ports,
                len(health_pre.get("trunks", [])),
                len(health_pre["cdp"]),
                len(health_pre["lldp"]),
                sum(1 for s in health_pre["env"].values() if _env_ok(s)),
                health_pre["reboot"].get("reason") or "unreported",
                extra=log,
            )
            for cls, count in (("CDP", len(health_pre["cdp"])), ("LLDP", len(health_pre["lldp"]))):
                if count == 0:
                    self.logger.info(
                        "Health: no %s neighbors before the upgrade — that "
                        "class is skipped in the post-check.",
                        cls,
                        extra=log,
                    )
            self._attach_health_artifact(f"health-pre_{device.name}.json", health_pre, log)

        # -- Config save before the reload: RPC-triggered activation reloads
        # never ask the CLI's 'configuration modified. Save?' question —
        # unsaved running-config changes are silently lost. The saved/unsaved
        # DETERMINATION was removed by decision (2026-07-10): its only source
        # is the SNMP-bridged CISCO-CONFIG-MAN-MIB, which hangs on devices
        # without snmp-server (this fleet), so the check could never answer.
        # What remains is the deliberate act (opt-in save, verified by the
        # device's own RPC result) and a static platform-fact reminder.
        if save_config:
            self._save_config(client, log)
        else:
            self.logger.info(
                "Reminder: the activation reload never prompts to save — "
                "unsaved running-config changes will be lost. The 'Save "
                "running-config before reload' option does the save for you.",
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

        # -- 5b. Optional post-commit save (opt-in; the soak-window trade-off
        # is documented in the README: saving normalizes startup to the new
        # OS's rendering, but an old-syntax startup is the safer rollback
        # path during soak — hence default OFF) -------------------------------
        if save_config_after:
            if committed:
                try:
                    self._save_config(client, log, context="after commit")
                except UpgradeAbort as exc:
                    raise UpgradeAbort(
                        f"The upgrade to {target_str} IS committed and the Nautobot "
                        f"sync was attempted{sync_note}, but the post-commit save "
                        f"failed: {exc}"
                    ) from exc
            else:
                self.logger.warning(
                    "Skipping the post-commit save: commit not yet confirmed.",
                    extra=log,
                )

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

        # -- Post-upgrade health report (opt-in, report-only by decision) ----
        if health_checks and health_pre is not None:
            if committed:
                try:
                    self._post_health_check(client, device, health_pre, log)
                except JobStopped:
                    # the ONLY post-commit stop checkpoint in the job: honor the
                    # stop by cutting the REPORT short — never by re-classifying
                    # a committed, synced upgrade as failed. The thread halts at
                    # the same safe boundary (this method returns just below).
                    self.logger.warning(
                        "Stop requested during the post-upgrade health check — "
                        "check cut short. The upgrade IS committed and synced; "
                        "compare against the pre-upgrade baseline artifact "
                        "manually.",
                        extra=log,
                    )
            else:
                self.logger.warning(
                    "Skipping the post-upgrade health check: commit not yet "
                    "confirmed (device state still in flux). The pre-upgrade "
                    "baseline artifact is attached for manual comparison.",
                    extra=log,
                )

        if not committed:
            return (
                f"Upgraded to {target_str}; commit issued but not yet confirmed — "
                f"verify with 'show install summary'.{sync_note}"
            )
        return f"Upgraded and committed to {target_str}.{sync_note}"

    def _handle_already_on_target(
        self, client, device, target_version, dryrun, log, run_scope="full"
    ):
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
        if run_scope != "full":
            # Stage scopes promise NO install activity (review finding: the
            # commit-repair is an install write and belongs to Full runs).
            self.logger.warning(
                "On target %s but not confirmed committed (state: %s). Run "
                "scope '%s' performs no install writes — run with scope "
                "'full' (or commit manually) to clear any pending "
                "auto-rollback.",
                target_str,
                tokens or "unknown",
                run_scope,
                extra=log,
            )
            return (
                f"On target {target_str} but not confirmed committed; scope "
                f"'{run_scope}' makes no install writes — run scope 'full' to "
                "commit."
            )
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
            committed = self._install_commit(client, target_str, log)
        except (JobStopped, EngineBusy):
            # Neither is a device-state verdict (review finding: swallowing
            # them reported an UNCOMMITTED device as handled): the stop must
            # end the run, and a busy engine means another operation is in
            # flight — both surface to the per-device handler unchanged.
            raise
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
        if committed:
            return f"On target {target_str}; install commit confirmed."
        return (
            f"On target {target_str}; install commit issued but not yet "
            "confirmed by the ledger — re-run to verify (auto-rollback, if "
            "armed, is cancelled by a successful commit)."
        )

    # -------------------------------------------------------- helpers: setup --

    def _capture_health_snapshot(self, client, want_trunks=False):
        """One health snapshot from pure oper reads (plus reboot reason).

        Raises RestconfError on any failed read — the PRE caller converts
        that to a fail-closed abort (a requested baseline must exist), the
        POST loop tolerates it as a transient and re-polls. want_trunks adds
        the one-time interface-CONFIG read that identifies trunk ports (pre
        only; trunk membership doesn't change during the upgrade window).
        """
        # interfaces and device-system ALWAYS exist on a live device, so a 404
        # (or an empty/coerced body) is a FAILED read, never "no interfaces" —
        # treating it as empty would fabricate a total-outage report against a
        # populated baseline. CDP/LLDP legitimately 404 when the feature is
        # off, and environment-sensors can be absent on virtual platforms;
        # those stay tolerant (empty env is additionally guarded at compare
        # time, never read as "every sensor vanished").
        interfaces = _parse_interfaces(client.get(C.DATA_INTERFACES_OPER, ok_404=False) or {})
        if not interfaces:
            raise RestconfError(
                "interfaces-oper returned no interfaces — treating the health "
                "snapshot as a failed read"
            )
        cdp = _parse_cdp(client.get(C.DATA_CDP_NEIGHBORS, ok_404=True) or {})
        lldp = _parse_lldp(client.get(C.DATA_LLDP_ENTRIES, ok_404=True) or {})
        env = _parse_env(client.get(C.DATA_ENV_SENSORS, ok_404=True) or {})
        system = (client.get(C.DATA_DEVICE_SYSTEM, ok_404=False) or {}).get(
            "Cisco-IOS-XE-device-hardware-oper:device-system-data", {}
        )
        snapshot = {
            "schema": 1,
            "interfaces": interfaces,
            "cdp": cdp,
            "lldp": lldp,
            "env": env,
            "reboot": {
                "reason": str(system.get("last-reboot-reason", "") or ""),
                "severity": str(system.get("reason-severity", "") or ""),
            },
        }
        if want_trunks:
            native_if = client.get(C.DATA_NATIVE_INTERFACE, ok_404=True) or {}
            snapshot["trunks"] = sorted(_parse_trunks(native_if, cdp))
        return snapshot

    def _post_health_check(self, client, device, health_pre, log):
        """Compare post-upgrade health against the pre-activation baseline.

        REPORT-ONLY by decision: findings are logged (trunk/infrastructure
        and environment findings at error level) and never un-succeed the
        committed upgrade. Convergence-aware: everything up-before must
        return within HEALTH_CONVERGENCE_TIMEOUT — ports renegotiate, APs
        boot on PoE, CDP ages in — so the check re-polls instead of judging
        one instant. An abnormal-reboot verdict comes from the device's own
        reason-severity leaf (state, not string-matching).
        """
        deadline = time.monotonic() + C.HEALTH_CONVERGENCE_TIMEOUT
        snap = None
        reg = None
        while True:
            try:
                snap = self._capture_health_snapshot(client)
                reg = _health_regressions(health_pre, snap)
                if _health_is_clean(reg):
                    break
            except RestconfError as exc:
                self.logger.warning(
                    "Health re-poll read failed (%s) — retrying until the convergence deadline.",
                    exc,
                    extra=log,
                )
            if time.monotonic() >= deadline:
                break
            self._check_stop()
            time.sleep(C.HEALTH_POLL_INTERVAL)

        trunks = set(health_pre.get("trunks", []))
        reboot = (snap or {}).get("reboot", {})
        if str(reboot.get("severity", "")).lower() == "abnormal":
            self.logger.error(
                "HEALTH: the device reports its post-upgrade reboot was "
                "ABNORMAL (reason: '%s') — investigate before trusting this "
                "upgrade window.",
                reboot.get("reason") or "unknown",
                extra=log,
            )
        elif reboot.get("reason"):
            self.logger.info(
                "Health: post-upgrade reboot reason '%s' (severity: %s).",
                reboot["reason"],
                reboot.get("severity") or "unreported",
                extra=log,
            )
        if snap is None or reg is None:
            self.logger.warning(
                "Health check could not complete: no successful post-upgrade "
                "snapshot within the convergence window — compare the pre "
                "artifact manually.",
                extra=log,
            )
            return
        errors, warnings_ = _classify_health(reg, trunks)
        for msg in errors:
            self.logger.error("HEALTH: %s.", msg, extra=log)
        for msg in warnings_:
            self.logger.warning("Health: %s.", msg, extra=log)
        pre_up = {
            n
            for n, s in health_pre.get("interfaces", {}).items()
            if s.get("admin") and s.get("oper")
        }
        now_up = {n for n, s in snap.get("interfaces", {}).items() if s.get("oper")}
        new_neighbors = [k for k in snap.get("cdp", {}) if k not in health_pre.get("cdp", {})] + [
            k for k in snap.get("lldp", {}) if k not in health_pre.get("lldp", {})
        ]
        preexisting_bad = [
            s for s, state in health_pre.get("env", {}).items() if not _env_ok(state)
        ]
        if (now_up - pre_up) or new_neighbors or preexisting_bad:
            self.logger.info(
                "Health notes (never findings): %d newly-up port(s), %d new "
                "neighbor(s), %d sensor(s) already unhealthy before the "
                "upgrade — detail in the artifacts.",
                len(now_up - pre_up),
                len(new_neighbors),
                len(preexisting_bad),
                extra=log,
            )
        if not errors and not warnings_:
            self.logger.info(
                "Health check clean: all %d baseline-up port(s), %d CDP and "
                "%d LLDP neighbor(s), and %d healthy sensor(s) are back.",
                sum(
                    1
                    for s in health_pre.get("interfaces", {}).values()
                    if s.get("admin") and s.get("oper")
                ),
                len(health_pre.get("cdp", {})),
                len(health_pre.get("lldp", {})),
                sum(1 for s in health_pre.get("env", {}).values() if _env_ok(s)),
                extra=log,
            )
        self._attach_health_artifact(f"health-post_{device.name}.json", snap, log)
        self._attach_health_artifact(
            f"health-report_{device.name}.json",
            {"schema": 1, "errors": errors, "warnings": warnings_, "reboot": reboot},
            log,
        )

    # --------------------------------------------------- helpers: read state --

    def _apply_avc_suppression(self, client, log):
        """Insert the SELinux AVC console filter into running-config (opt-in).

        The %SELINUX-1-VIOLATION console bursts are documented SELinux
        alerts — benign in our testing, though NOT a Cisco-confirmed cosmetic
        defect (see the README's SELinux section) — a side effect of how smand
        watches files whenever anything (this job, or a human 'show' command)
        builds a filesystem listing. They clutter the
        physical console and terminal-monitor (SSH) sessions during an
        upgrade. When ticked, the job inserts 'logging discriminator NBAVC
        facility drops SELINUX' and attaches it to the TERMINALS only. The
        'show logging' buffer and syslog hosts deliberately stay unfiltered
        (they are the record; genuine SELinux events share this facility and
        remain fully visible there). Properties, all deliberate:
          * Applied on EVERY release regardless of version — the messages
            are not tied to one train (field-observed on 17.15.x AND on
            17.18.3, which an earlier read had wrongly assumed was fixed).
          * UNSAVED: running-config only; the activation reload erases it
            unless 'Save running-config before reload' is also ticked on a
            Full run (stage scopes never save — documented on the checkbox).
          * READ-BEFORE-WRITE, fail-CLOSED, never clobbers: a foreign
            discriminator attached to console, an operator-owned NBAVC entry
            with different content, 'no logging console', filtered/XML
            console modes, or an unrecognizable config shape all skip with a
            warning.
          * NON-FATAL: log-only — every failure warns and the run proceeds.
          * Ordering: entry PATCH before attach PATCH (the model's rule);
            a partial failure names the residue it leaves.
        """
        name = C.AVC_DISCRIMINATOR_NAME
        try:
            raw = client.get(C.DATA_NATIVE_LOGGING, ok_404=True)
        except RestconfError as exc:
            self.logger.warning(
                "Could not read the logging config to apply AVC suppression "
                "(%s) — continuing without it (console may show SELinux "
                "bursts).",
                exc,
                extra=log,
            )
            return
        if not raw:
            cfg = {}  # 404/empty: no logging config exists — safe to create
        elif isinstance(raw, dict) and isinstance(raw.get("Cisco-IOS-XE-native:logging"), dict):
            cfg = raw["Cisco-IOS-XE-native:logging"]
        else:
            # Unrecognizable shape: a read-before-write guard must fail
            # CLOSED — writing blind is the exact clobber the read prevents
            # (review finding).
            self.logger.warning(
                "Logging config came back in an unrecognized shape — AVC "
                "suppression skipped (never writing blind).",
                extra=log,
            )
            return

        # Ownership: an existing NBAVC ENTRY only counts as ours when its
        # content is exactly our filter — otherwise merging would rewrite an
        # operator's own discriminator semantics (review finding).
        ours_entry = {"name": name, "facility": {"drops": "SELINUX"}}
        entry_conflict = None
        for entry in cfg.get("discriminator") or []:
            if isinstance(entry, dict) and str(entry.get("name")) == name:
                if entry != ours_entry:
                    entry_conflict = entry
                break

        # TERMINALS ONLY — the two destinations humans watch live: the
        # physical console and terminal-monitor (SSH) sessions. The 'show
        # logging' buffer and syslog hosts stay UNFILTERED by decision
        # (2026-07-10): they are the record; the terminals are the noise.
        # Both attach points live inside YANG CHOICES, so merging blindly
        # could flip an operator's 'no logging console/monitor' or
        # filtered/XML mode — each destination is skipped (warned) unless
        # attaching is a pure addition.
        attach = {}
        skipped = []
        already = []

        console_cfg = cfg.get("console-config") or {}
        console_cc = (console_cfg.get("common-config") or {}).get("console") or {}
        console_attached = str((console_cc.get("discriminator") or {}).get("name", ""))
        if console_attached == name:
            already.append("console")
        elif console_attached:
            skipped.append(f"console (operator discriminator '{console_attached}' attached)")
        elif console_cfg.get("console") is False:
            skipped.append("console ('no logging console' is configured)")
        elif "filtered" in console_cc or "xxml" in console_cc:
            skipped.append("console (filtered/XML logging mode)")
        else:
            attach["console-config"] = {
                "common-config": {"console": {"discriminator": {"name": name}}}
            }

        # NOTE: target monitor-config, never the legacy 'monitor' container
        # (status obsolete in the model).
        monitor_cfg = cfg.get("monitor-config") or {}
        monitor_cc = (monitor_cfg.get("common-config") or {}).get("monitor") or {}
        monitor_attached = str((monitor_cc.get("discriminator") or {}).get("name", ""))
        if monitor_attached == name:
            already.append("monitor")
        elif monitor_attached:
            skipped.append(f"monitor (operator discriminator '{monitor_attached}' attached)")
        elif monitor_cfg.get("monitor") is False:
            skipped.append("monitor ('no logging monitor' is configured)")
        elif "filtered" in monitor_cc or "xxml" in monitor_cc:
            skipped.append("monitor (filtered/XML logging mode)")
        else:
            attach["monitor-config"] = {
                "common-config": {"monitor": {"discriminator": {"name": name}}}
            }

        if entry_conflict is not None:
            self.logger.warning(
                "AVC suppression skipped: an operator-owned 'logging "
                "discriminator %s' already exists with different content — "
                "existing operator logging config is never replaced.",
                name,
                extra=log,
            )
            return
        if skipped:
            self.logger.warning(
                "AVC suppression skipped for %s — existing operator logging "
                "config is never replaced.",
                "; ".join(skipped),
                extra=log,
            )
        if not attach:
            if not skipped:
                self.logger.info(
                    "SELinux AVC suppression already in place (discriminator %s attached to %s).",
                    name,
                    ", ".join(already) or "all destinations",
                    extra=log,
                )
            return
        try:
            # Entry first (the model's own rule: created first, deleted last).
            client.patch(
                C.DATA_NATIVE_LOGGING,
                {"Cisco-IOS-XE-native:logging": {"discriminator": [ours_entry]}},
            )
        except RestconfError as exc:
            self.logger.warning(
                "Applying AVC suppression failed before any change landed "
                "(%s) — continuing without it.",
                exc,
                extra=log,
            )
            return
        try:
            client.patch(C.DATA_NATIVE_LOGGING, {"Cisco-IOS-XE-native:logging": attach})
        except RestconfError as exc:
            self.logger.warning(
                "AVC suppression partially applied: the 'logging "
                "discriminator %s' entry LANDED in running-config but the "
                "destination attach failed (%s). The unattached entry is "
                "harmless; remove it with 'no logging discriminator %s' if "
                "unwanted. Continuing.",
                name,
                exc,
                name,
                extra=log,
            )
            return
        self.logger.info(
            "SELinux noise filter applied to running-config (logging "
            "discriminator %s, facility drops SELINUX; terminals quieted: "
            "%s). 'show logging' and syslog hosts still record everything. "
            "UNSAVED: the reload erases it unless 'Save running-config "
            "before reload' is ticked on this Full run.",
            name,
            ", ".join(sorted(already + [k.replace("-config", "") for k in attach])),
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
                if len(roster) > 1:
                    self.logger.info(
                        "All %d stack members rejoined after the reload.",
                        len(roster),
                        extra=log,
                    )
                else:
                    self.logger.info(
                        "The chassis rejoined after the reload (roster complete).",
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


# --------------------------------------------------------- module utilities --
