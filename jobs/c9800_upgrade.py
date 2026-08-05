"""Catalyst 9800 WLC upgrade with AP image predownload — RESTCONF only.

The sibling of the switch job (iosxe_upgrade.py), sharing its install-engine
layer (install_engine.InstallEngineMixin) and its doctrine: decisions from
device-published state, timers only ever DECLARE FAILURE, fail closed, every
device fact positively confirmed. What differs is what "full" costs: on a
switch it reloads one box; on a controller it reloads the box AND reboots
every joined AP. The centerpiece here is therefore **AP image predownload** —
push the target image to every AP's backup partition BEFORE the reload, so
APs come back with a partition swap instead of a download.

Run scopes (prefix chain, safe step is the default, exactly one choice
reloads):
  stage-copy    Step 1: image onto controller bootflash        zero impact
  stage-add     Steps 1&2: + install add                       zero impact
  predownload   Steps 1-3: + AP fleet holds the image, STOP    zero impact
  full          + activate -> reload -> commit          EVERY AP REBOOTS

v1 scope (deliberate, user-declared environment 2026-08): STANDALONE
controllers only — an HA SSO pair is refused by the topology gate with the
exact reading named (the whitelist widens when SSO hardware is available to
validate against). N+1/rolling AP migration, EWC, mesh APs, site-filter
staggered upgrades, and ISSU are out of scope and refused or unhandled
loudly, never silently.

The predownload contract (every rule below is bench-earned, 2026-08-03/04,
C9800-CL 17.15.5 -> 17.18.3, live APs, port-pull negative case):
  * The roster snapshot R0 (capwap-data at fire time) IS the contract.
    predownload-data is an ACTIVITY list — empty until engagement, and an
    AP whose CAPWAP session dies mid-download VANISHES from it (it does
    NOT flip to failed). A gate over "entries currently present" would
    pass wrongly; the gate is over R0.
  * Per-AP completion = present in predownload-data + pre-dwnld-complete +
    predownload-version equal to the LEARNED target quad. The quad is
    learned from the device after `install add` (prepare-location minus
    active-location — the version STRING cannot express a rebuild letter
    or the AP build field, so the string never gates). The AP's
    backup-sw-version flipping to the same quad is the corroborator and
    the already-held SKIP tier — same doctrine as the transfer pre-check.
  * pre-dwnld-failed mid-window means the AP is RETRYING (device retry
    machinery is real) — keep watching; failed AT the deadline is failure.
  * The deadline only ever declares failure, and names every incomplete
    AP. Nothing here expires into success.
"""

import re
import threading
import time
import uuid as uuid_lib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from celery import current_task
from celery.exceptions import SoftTimeLimitExceeded
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
from nautobot.dcim.models import Device, DeviceType, Location, Platform, SoftwareVersion
from nautobot.extras.models import DynamicGroup, Role, SecretsGroup, Status, Tag

from . import constants as C
from .install_engine import (
    InstallEngineMixin,
    JobStopped,
    UpgradeAbort,
    XcopyFailed,
    _auth_hint,
    _env_ok,
    _fmt_duration,
    _is_committed,
    _oper_entries,
    _parse_env,
    _redact_url,
    _version_key,
    _xcopy_url_unsupported,
)
from .restconf import RestconfClient, RestconfError

try:  # celery internals used to bind worker-thread logs (same as the switch job)
    from celery.app import pop_current_task, push_current_task
except Exception:  # noqa: BLE001 - defensive: run without thread-log binding
    pop_current_task = None
    push_current_task = None

name = "IOS-XE Upgrades"


# --- pure helpers (battery-covered; fixtures are the real bench captures) ----


def _quad(container):
    """(version, release, maint, build) ints from a version-info container.

    The wireless models publish versions as four uint8 leaves, not strings
    (predownload-version, backup-sw-version — bench-confirmed shapes).
    Returns None when any field is absent/non-numeric.
    """
    if not isinstance(container, dict):
        return None
    try:
        return tuple(int(container[k]) for k in ("version", "release", "maint", "build"))
    except (KeyError, TypeError, ValueError):
        return None


def _quad_from_str(text):
    """(a, b, c, d) ints from a dotted image-version string, else None.

    prepare/active-location publish image-version as a STRING quad
    ("17.18.3.18"), unlike the int containers elsewhere.
    """
    m = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)\.(\d+)\s*$", str(text or ""))
    if not m:
        return None
    return tuple(int(m.group(i)) for i in (1, 2, 3, 4))


def _bundle_quads(payload, list_name):
    """Every image-version quad a prepare/active-location list publishes."""
    quads = set()
    for entry in _oper_entries(payload, list_name):
        for img in entry.get("image-data") or []:
            quad = _quad_from_str(img.get("image-version"))
            if quad:
                quads.add(quad)
    return quads


def _learned_target_quad(prepare_payload, active_payload):
    """(quad, detail): the STAGED bundle's AP image quad, device-published.

    The version STRING cannot express what the AP quads need (a rebuild
    letter has no numeric form, and even a plain target says nothing about
    the AP image build field) — but after `install add` the device itself
    publishes the staged bundle's exact quad in prepare-location
    (bench-proven for 17.18.03 -> 17.18.3.18). The staged quad is the set
    difference prepare-minus-active; anything but exactly ONE candidate is
    a refusal, never a guess. State over inference: the string was the
    inference, this is the state.
    """
    prepare = _bundle_quads(prepare_payload, "ap-image-prepare-location")
    active = _bundle_quads(active_payload, "ap-image-active-location")
    staged = prepare - active
    base = (
        f"prepare-location publishes {sorted(prepare) or 'nothing'}, "
        f"active-location {sorted(active) or 'nothing'}"
    )
    if len(staged) == 1:
        return staged.pop(), base
    if not staged and prepare:
        # 0 candidates with a populated prepare read: the staged bundle
        # publishes no AP image version outside the active set — possibly a
        # rebuild shipping IDENTICAL AP images (unbenched corner; if ever
        # proven, the fix is a device-verified prepare-subset tier, not an
        # override flag).
        return None, (
            f"{base} — the staged bundle publishes no AP image version "
            "outside the active set (identical AP images in a rebuild?)"
        )
    return None, (
        f"{base} — {len(staged)} staged candidate(s)"
        + (
            "; a second staged bundle? 'Clean device first' clears staged software"
            if len(staged) > 1
            else ""
        )
    )


def _ap_roster(capwap_payload):
    """{wtp-mac: {name, model, oper, sw, backup_quad}} from a capwap read.

    Only JOINED APs appear in capwap-data (device fact); an empty dict from
    a positively-read empty list is meaningful (backup controller with no
    APs) and distinct from an unreadable list (caller's job to separate).
    """
    roster = {}
    for entry in _oper_entries(capwap_payload, "capwap-data"):
        mac = entry.get("wtp-mac")
        if not mac:
            continue
        detail = entry.get("device-detail") or {}
        wtp_version = detail.get("wtp-version") or {}
        static = detail.get("static-info") or {}
        roster[str(mac).lower()] = {
            "name": entry.get("name") or str(mac),
            "model": (static.get("ap-models") or {}).get("model") or "",
            "oper": (entry.get("ap-state") or {}).get("ap-operation-state") or "",
            "sw": wtp_version.get("sw-version") or "",
            "backup_quad": _quad(wtp_version.get("backup-sw-version")),
        }
    return roster


def _predownload_states(payload):
    """{wtp-mac: (pred-status, quad)} from a predownload-data read."""
    states = {}
    for entry in _oper_entries(payload, "predownload-data"):
        mac = entry.get("wtp-mac")
        if not mac:
            continue
        states[str(mac).lower()] = (
            str(entry.get("pred-status") or ""),
            _quad(entry.get("predownload-version")),
        )
    return states


def _strip_reg_suffix(model):
    """AP model without the regulatory-domain suffix (C9130AXI-B -> C9130AXI).

    The bundle's ap-model-list omits the suffix while capwap-data carries it
    (bench-observed mismatch, 2026-08-03).
    """
    return re.sub(r"-[A-Z]{1,3}$", "", str(model or ""))


def _model_map(prepare_payload, target_quad):
    """{base-model: image-name} for the STAGED bundle from prepare-location.

    prepare-location publishes one entry per bundle on box; only image-data
    rows at the LEARNED target quad count — the running release's rows must
    never vouch for the staged one.
    """
    covered = {}
    for entry in _oper_entries(prepare_payload, "ap-image-prepare-location"):
        for img in entry.get("image-data") or []:
            if _quad_from_str(img.get("image-version")) != target_quad:
                continue
            for model in img.get("ap-model-list") or []:
                covered[str(model)] = img.get("image-name") or "?"
    return covered


def _standalone_verdict(stack_payload):
    """(ok, detail) — POSITIVE standalone identification from stack-oper.

    Bench-earned (2026-08-03): a standalone 9800-CL publishes stack-mode
    'mode-active-standby', topology 'one-plus-one', and sso-ready-flag
    False as CONSTANTS — none of them indicate a pair. The real standalone
    signature is exactly ONE stack-node with role-active + state-ready.
    Anything else (unreadable, empty, multiple nodes, odd role/state) is
    NOT standalone and the detail names exactly what was read.
    """
    if not stack_payload:
        return False, "stack-oper-data unreadable or empty (silence is not standalone)"
    nodes = list(_oper_entries(stack_payload, "stack-node"))
    if not nodes:
        return False, "stack-oper-data carries no stack-node entries"
    if len(nodes) != 1:
        seen = [(n.get("role"), n.get("node-state")) for n in nodes]
        return False, f"{len(nodes)} chassis entries {seen} — a pair/stack, not standalone"
    role = nodes[0].get("role")
    state = nodes[0].get("node-state")
    if role == "role-active" and state == "state-ready":
        return True, "one chassis, role-active, state-ready"
    return False, f"single chassis but role={role!r} state={state!r}"


def _completion_report(r0, pred_states, roster_now, target_quad, allow_unsupported):
    """Per-AP verdicts for the R0 contract: {mac: (satisfied, state, why)}.

    The vocabulary is deliberately closed:
      complete        pred entry complete at the target quad
      already-held    no pred entry, but the AP's backup partition already
                      holds the target (idempotent re-run / fleet re-check —
                      same skip doctrine as the transfer pre-check)
      unsupported     device says this model cannot predownload; satisfied
                      ONLY behind allow_unsupported_aps (named either way)
      in-progress / initiated / none    engaged or pending — keep waiting
      failed          device-reported failure; the AP RETRIES (bench+docs),
                      so this only condemns at the deadline
      wrong-version   pred entry complete but NOT the target — a stale
                      record from an earlier attempt; never satisfied
      vanished        in R0 but now absent from BOTH lists (bench: the
                      port-pull signature) — never satisfied
      not-engaged     joined but no pred entry yet — normal early (WNCD
                      batching), incomplete at the deadline
    """
    report = {}
    for mac in r0:
        pred = pred_states.get(mac)
        now = roster_now.get(mac)
        if pred is not None:
            status, quad = pred
            if status == "pre-dwnld-complete":
                if quad is not None and quad == target_quad:
                    report[mac] = (True, "complete", f"quad {quad}")
                else:
                    report[mac] = (False, "wrong-version", f"complete but quad {quad}")
            elif status == "pre-dwnld-unsupported":
                report[mac] = (
                    bool(allow_unsupported),
                    "unsupported",
                    "device reports predownload unsupported",
                )
            elif status == "pre-dwnld-failed":
                report[mac] = (False, "failed", "device-reported failure (AP retries)")
            else:
                report[mac] = (False, status.replace("pre-dwnld-", "") or "unknown", "engaged")
        elif now is not None:
            if now.get("backup_quad") is not None and now.get("backup_quad") == target_quad:
                report[mac] = (True, "already-held", f"backup partition {now['backup_quad']}")
            else:
                report[mac] = (False, "not-engaged", "joined, no predownload entry yet")
        else:
            report[mac] = (
                False,
                "vanished",
                "left the joined roster mid-window (absent from both lists)",
            )
    return report


def _coerce_bool(field, value):
    """A real boolean from a stored kwarg, or a LOUD refusal.

    Stored ScheduledJob/API kwargs bypass form coercion, and the string
    'false' is truthy — the exact failure the Dev Tester's confirm_writes
    identity check exists for. None means 'field absent' and maps to False.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no", ""):
        return False
    raise UpgradeAbort(
        f"Input {field!r} received {value!r} — not a boolean (stale ScheduledJob "
        "kwargs?). Refusing rather than guessing which way a safety gate flips."
    )


def _report_counts(report):
    return Counter(state for _, state, _ in report.values())


def _named(report, r0, wanted_states=None, satisfied=None):
    """Sorted 'Name (mac): state — why' lines, filtered by state/satisfaction."""
    lines = []
    for mac, (ok, state, why) in sorted(report.items()):
        if wanted_states is not None and state not in wanted_states:
            continue
        if satisfied is not None and ok is not satisfied:
            continue
        label = r0.get(mac, {}).get("name", mac)
        lines.append(f"{label} ({mac}): {state} — {why}")
    return lines


class C9800Upgrade(InstallEngineMixin, Job):
    """Upgrade Catalyst 9800 wireless controllers with AP image predownload."""

    location = MultiObjectVar(
        model=Location, required=False, description="Scope the device picker."
    )
    role = MultiObjectVar(model=Role, required=False, description="Scope the device picker.")
    status = MultiObjectVar(model=Status, required=False, description="Scope the device picker.")
    platform = MultiObjectVar(
        model=Platform, required=False, description="Scope the device picker."
    )
    device_type = MultiObjectVar(
        model=DeviceType, required=False, description="Scope the device picker."
    )
    current_version = MultiObjectVar(
        model=SoftwareVersion, required=False, description="Scope the device picker."
    )
    tags = MultiObjectVar(model=Tag, required=False, description="Scope the device picker.")
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
        description="Controllers to upgrade. A Full run requires devices picked HERE.",
    )
    dynamic_groups = MultiObjectVar(
        model=DynamicGroup,
        required=False,
        query_params={"content_type": "dcim.device"},
        description=(
            "Device groups, resolved live at run start — accepted for the staging and "
            "predownload scopes. A Full run REFUSES group-driven rosters: which campus "
            "reloads tonight is a named decision, never a filter result."
        ),
    )
    target_version = ObjectVar(
        model=SoftwareVersion,
        description="Target IOS-XE release (its image must be registered and mapped).",
    )
    run_scope = ChoiceVar(
        choices=(
            ("stage-copy", "Step 1 - Copy image to controller (default)"),
            ("stage-add", "Steps 1 & 2 - Copy image and prep (install add)"),
            ("predownload", "Steps 1-3 - Stage + AP predownload (stops before any reload)"),
            ("full", "Full - Activate: reloads controller AND every joined AP"),
        ),
        default="stage-copy",
        description=(
            "How far to go. 'Predownload' is the schedulable, zero-impact goal state: "
            "every joined AP holds the target image and the window run needs only "
            "activate -> reload -> commit."
        ),
    )
    clean_before = BooleanVar(
        default=False,
        label="Clean device first (remove inactive/staged software)",
        description="Deliberate override of the staged-conflict stop, before the space gate.",
    )
    save_config = BooleanVar(
        default=False,
        label="Save running-config before reload",
        description="The activation reload never prompts to save.",
    )
    save_config_after = BooleanVar(
        default=False,
        label="Save running-config after commit",
        description="Soak trade-off: an old-syntax startup is the safer rollback during soak.",
    )
    gc_backup = BooleanVar(
        default=False,
        label="Golden Config backup (before & after)",
        description="Fail-closed before, warn-only after — same contract as the switch job.",
    )
    health_checks = BooleanVar(
        default=False,
        label="Wireless health checks (report-only)",
        description=(
            "Baseline before activation, compare after commit: AP roster as a named set "
            "difference, per-AP version and operation state, radio states (only radios "
            "up in the baseline), controller reload reason, environment. Client counts "
            "and RF/RRM metrics are deliberately excluded (post-reboot re-convergence "
            "makes them false-positive machines)."
        ),
    )
    transfer_method = ChoiceVar(
        choices=(
            ("xcopy", "Async xcopy (default - classic-copy fallback)"),
            ("copy", "Classic copy only"),
        ),
        default="xcopy",
        description="Same transfer tiers as the switch job (bench-proven on the 9800-CL).",
    )
    secrets_group_override = ObjectVar(
        model=SecretsGroup,
        required=False,
        description="Override the device's own Secrets Group for RESTCONF credentials.",
    )
    remove_inactive = BooleanVar(
        default=False,
        label="Remove inactive software after commit",
        description="Reclaims controller flash after a confirmed commit (Cisco's step 0).",
    )
    parallelism = IntegerVar(
        default=1,
        min_value=1,
        max_value=C.MAX_PARALLELISM,
        description=(
            "Concurrent controllers for the staging/predownload scopes. A Full run "
            "always runs one controller at a time regardless of this value."
        ),
    )
    predownload_deadline_minutes = IntegerVar(
        default=120,
        min_value=5,
        max_value=1440,
        label="Predownload deadline (minutes)",
        description=(
            "How long to wait for every joined AP to hold the target image. Real "
            "fleets range minutes to hours (size, WAN, WNCD batching). The deadline "
            "only ever DECLARES FAILURE, naming each incomplete AP — it never "
            "expires into success."
        ),
    )
    tolerate_missing_aps = BooleanVar(
        default=False,
        label="Proceed despite incomplete APs (Full scope)",
        description=(
            "Named-exceptions escape for the strict predownload gate: activate even if "
            "some snapshot-roster APs are incomplete or vanished. Every such AP is "
            "named and takes the SLOW post-reload download path."
        ),
    )
    allow_unsupported_aps = BooleanVar(
        default=False,
        label="Allow predownload-unsupported AP models",
        description=(
            "Proceed when the device reports pre-dwnld-unsupported or the staged "
            "bundle's model map lacks a joined AP's model. Those APs are named and "
            "will take the slow post-reload path (or fail to join the new release)."
        ),
    )
    debug = BooleanVar(default=False, description="Verbose RESTCONF debug logging.")
    dryrun = DryRunVar(
        description=(
            "Read-only preview: gates, topology verdict, and the wireless blast-radius echo."
        )
    )

    class Meta:
        name = "Cisco 9800 WLC Upgrade (IOS-XE)"
        description = (
            "Upgrade Catalyst 9800 wireless controllers over RESTCONF with AP image "
            "predownload: stage, push the image to every joined AP's backup partition, "
            "prove per-AP completion from device-published state, then (Full only) "
            "activate, reload, and commit. v1 supports STANDALONE controllers; HA SSO "
            "pairs are refused by the topology gate. Validated on 9800-CL."
        )
        has_sensitive_variables = False
        dryrun_default = True
        # Budget math (review finding): the predownload deadline input allows
        # up to 1440 min (24 h) of watch, and the transfer window plus install
        # add can precede it — the Celery limits must contain the WHOLE stack
        # or the soft limit replaces the deadline's named-AP failure with an
        # anonymous cooperative stop. 1440*60 + ~2 h of stack/slack:
        soft_time_limit = 93600
        time_limit = 94800

    # ------------------------------------------------------------------ run --

    def run(  # noqa: PLR0913 - the form IS the interface
        self,
        *,
        location=None,
        role=None,
        status=None,
        platform=None,
        device_type=None,
        current_version=None,
        tags=None,
        devices=None,
        dynamic_groups=None,
        target_version=None,
        run_scope="stage-copy",
        clean_before=False,
        save_config=False,
        save_config_after=False,
        gc_backup=False,
        health_checks=False,
        transfer_method="xcopy",
        secrets_group_override=None,
        remove_inactive=False,
        parallelism=1,
        predownload_deadline_minutes=120,
        tolerate_missing_aps=False,
        allow_unsupported_aps=False,
        debug=False,
        dryrun=False,
    ):
        log_success = getattr(self.logger, "success", self.logger.info)
        results = {}
        failed = []
        self._stop = threading.Event()

        valid_scopes = {"stage-copy", "stage-add", "predownload", "full"}
        if run_scope not in valid_scopes:
            # Stored ScheduledJob kwargs bypass form validation (hard-won
            # switch-job lesson) — refuse loudly, never reroute stored intent.
            raise UpgradeAbort(
                f"Unknown run scope {run_scope!r} (stale ScheduledJob kwargs?). "
                f"Valid: {sorted(valid_scopes)}"
            )
        if target_version is None:
            raise UpgradeAbort("No target version selected.")
        # Same stored-kwargs lesson for BOOLEANS: the string 'false' is truthy,
        # and two of these checkboxes widen the blast radius when they flip
        # open (Dev Tester's confirm_writes precedent — identity, not truth).
        clean_before = _coerce_bool("clean_before", clean_before)
        save_config = _coerce_bool("save_config", save_config)
        save_config_after = _coerce_bool("save_config_after", save_config_after)
        gc_backup = _coerce_bool("gc_backup", gc_backup)
        health_checks = _coerce_bool("health_checks", health_checks)
        remove_inactive = _coerce_bool("remove_inactive", remove_inactive)
        tolerate_missing_aps = _coerce_bool("tolerate_missing_aps", tolerate_missing_aps)
        allow_unsupported_aps = _coerce_bool("allow_unsupported_aps", allow_unsupported_aps)
        debug = _coerce_bool("debug", debug)
        dryrun = _coerce_bool("dryrun", dryrun)

        # Bind worker-thread logs to the JobResult (both Celery thread-locals —
        # see the switch job's run() for the field-debugged reasoning).
        celery_task = None
        celery_request = None
        if current_task is not None:
            try:
                celery_task = current_task._get_current_object()
                if celery_task is not None:
                    request = celery_task.request
                    celery_request = request if getattr(request, "id", None) else None
            except Exception:  # noqa: BLE001 - no task context (tests, shell)
                celery_task = None
                celery_request = None

        device_list = self._resolve_roster(devices, dynamic_groups)

        # --- Full-scope policy: the service-level guardrails ----------------
        # Every per-device gate protects the DEVICE; on a WLC the thing at
        # risk is the SERVICE. Full is therefore: explicitly-picked devices
        # only, one controller per run, one at a time.
        if run_scope == "full":
            if dynamic_groups:
                raise UpgradeAbort(
                    "Full scope refuses Dynamic Group rosters: group membership "
                    "resolves at run time by design, and 'which campus reloads "
                    "tonight' must be a named decision. Pick the controller "
                    "explicitly in the Devices field."
                )
            if len(device_list) > 1:
                raise UpgradeAbort(
                    f"Full scope upgrades ONE controller per run ({len(device_list)} "
                    "selected). Every joined AP on a controller reboots with it — "
                    "run additional controllers as separate, deliberate runs."
                )

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
            "Starting Catalyst 9800 upgrade to **%s** for %d controller(s), scope "
            "'%s'%s — nautobot-upgrades v%s.",
            target_version,
            len(device_list),
            run_scope,
            " (DRY-RUN)" if dryrun else "",
            C.JOB_VERSION,
        )

        def _one_device(device):
            if celery_task is not None and push_current_task is not None:
                push_current_task(celery_task)
                if celery_request is not None:
                    celery_task.request_stack.push(celery_request)
            close_old_connections()
            device_started = time.monotonic()
            try:
                summary = self._upgrade_controller(
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
                    health_checks,
                    transfer_method,
                    predownload_deadline_minutes,
                    tolerate_missing_aps,
                    allow_unsupported_aps,
                )
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

        if gc_backup:
            if dryrun:
                self.logger.info(
                    "DRY-RUN: would run the Golden Config backup for %d controller(s) "
                    "before and after.",
                    len(device_list),
                )
            else:
                self._run_gc_backup(device_list, "before")

        workers = max(1, min(int(parallelism or 1), C.MAX_PARALLELISM, len(device_list) or 1))
        if run_scope == "full" and workers != 1:
            self.logger.info(
                "Full scope runs ONE controller at a time (parallelism input "
                "ignored for the reload scope)."
            )
            workers = 1
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="c9800-upgrade")
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
            self._stop.set()
            self.logger.error(
                "Stop signal received — stopping in-flight controller upgrades at "
                "their next safe checkpoint..."
            )
            executor.shutdown(wait=True, cancel_futures=True)
            raise

        if gc_backup and not dryrun:
            try:
                self._run_gc_backup(device_list, "after")
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - warn-only by contract
                self.logger.warning(
                    "Golden Config backup (after) failed: %s — completed upgrades are unaffected.",
                    exc,
                )

        if failed:
            raise RuntimeError(
                f"{len(failed)} of {len(device_list)} controller(s) failed: "
                f"{', '.join(sorted(failed))} — see the per-device errors in the job log."
            )
        return results

    # -------------------------------------------------------- orchestrate --

    def _upgrade_controller(
        self,
        device,
        target_version,
        override_group,
        remove_inactive,
        debug,
        dryrun,
        run_scope,
        clean_before,
        save_config,
        save_config_after,
        health_checks,
        transfer_method,
        deadline_minutes,
        tolerate_missing_aps,
        allow_unsupported_aps,
    ):
        log = {"object": device}

        # -- 0. Credentials + reachability + idempotency ---------------------
        host = self._device_host(device)
        username, password = self._credentials(device, override_group, log)
        client = RestconfClient(
            host, username, password, logger=self.logger, log_object=device, debug=debug
        )
        data = self._check_reachable(client, host)
        self.logger.info("RESTCONF reachable and authenticated at %s.", host, extra=log)
        current = self._extract_version(data)
        self.logger.info("Current version: **%s**.", current or "unknown", extra=log)
        target_str = target_version.version
        if _version_key(current) and _version_key(current) == _version_key(target_str):
            # 'On target' is NOT 'committed' (review finding, HIGH): the
            # commit-failure abort tells the operator to re-run this job, and
            # on a WLC an uncommitted image is a ticking auto-abort that
            # reverts the WHOLE AP FLEET. Mirror the switch job's
            # commit-to-be-safe re-run contract.
            tokens = self._state_tokens(client, target_str)
            if _is_committed(tokens):
                return (
                    f"Already on {target_str} and committed — nothing to do. "
                    "(Predownload for a FUTURE release is a separate run with "
                    "that release as the target.)"
                )
            if dryrun:
                return (
                    f"DRY-RUN: already running {target_str} but NOT confirmed "
                    "committed — a real Full run would commit (cancelling any "
                    "pending auto-abort rollback)."
                )
            if run_scope == "full":
                self.logger.warning(
                    "Running %s but NOT confirmed committed — committing now "
                    "(cancels any pending auto-abort rollback; a rollback would "
                    "reboot every predownloaded AP a second time).",
                    target_str,
                    extra=log,
                )
                committed = self._install_commit(client, target_str, log)
                if committed:
                    self._sync_nautobot(device, target_version, log)
                    return f"COMMITTED: {target_str} was activated but uncommitted; now committed."
                return (
                    f"Running {target_str}; commit issued but NOT positively "
                    "confirmed — verify with 'show install summary'."
                )
            self.logger.warning(
                "Running %s but NOT confirmed committed. Staging scopes do not "
                "commit — run scope 'Full' (it will commit without reloading "
                "anything already active) before the auto-abort timer expires.",
                target_str,
                extra=log,
            )
            return (
                f"Already on {target_str} but NOT confirmed committed — re-run "
                "with scope 'Full' to commit."
            )

        # -- 1. Pre-flight gates ---------------------------------------------
        self._gate_version_floor(current, log)
        self._gate_install_mode(client, log)

        # -- 2. Topology: positive standalone identification ------------------
        stack = client.get(C.DATA_STACK_OPER, ok_404=True)
        standalone, topo_detail = _standalone_verdict(stack)
        if standalone:
            self.logger.info("Topology gate: standalone (%s).", topo_detail, extra=log)
        elif run_scope in ("predownload", "full"):
            raise UpgradeAbort(
                f"Topology gate: NOT positively standalone — {topo_detail}. This job "
                "version supports standalone controllers only (HA SSO orchestration "
                "is unvalidated); silence or a pair reading refuses rather than "
                "guesses."
            )
        else:
            self.logger.warning(
                "Topology gate: NOT positively standalone (%s). Staging is harmless "
                "and proceeds; the predownload and Full scopes would refuse this "
                "controller.",
                topo_detail,
                extra=log,
            )

        # -- 3. Wireless blast-radius echo (all scopes, incl. dry-run) --------
        roster = self._read_roster(client, log)
        self._blast_radius_echo(client, roster, log)

        image = self._resolve_image(device, target_version, log)
        # Stale-choice guard + pre-fire xcopy guards (same rules as the switch
        # job; the engine-download method never existed here).
        if transfer_method in (None, ""):
            transfer_method = "xcopy"
        elif transfer_method not in ("xcopy", "copy"):
            raise UpgradeAbort(
                f"Unknown transfer method {transfer_method!r} (stale ScheduledJob "
                "kwargs?). Valid: xcopy (default), copy."
            )
        if transfer_method == "xcopy":
            reason = None
            if not image.image_file_size:
                reason = "the image has no recorded file size"
            else:
                url_problem = _xcopy_url_unsupported(image.download_url)
                if url_problem:
                    reason = f"the image URL carries {url_problem}"
            if reason:
                transfer_method = "copy"
                self.logger.warning(
                    "Async xcopy is unavailable for this image: %s — using the classic copy tier.",
                    reason,
                    extra=log,
                )

        entries, staged = self._inventory_other_versions(client, target_str)
        if staged:
            self.logger.info(
                "Install DB also tracks other version(s): %s. Staged: %s — if this "
                "run targets something else, check for a change in flight (clean "
                "with 'Clean device first' only if you OWN this device's change).",
                "; ".join(entries),
                " / ".join(staged),
                extra=log,
            )

        if not dryrun and clean_before:
            self._clean_device(client, target_str, log)

        # Filesystem discovery + free-space gate run in dry-run too (read-only)
        # so 'all pre-flight gates passed' means ALL of them (review finding —
        # a dry-run-green window run must not die on flash space).
        target_fs, partitions = self._discover_target_fs(client, log)
        self._gate_free_space(client, image, log, target_fs, partitions)

        if dryrun:
            plan = {
                "stage-copy": f"would copy '{_redact_url(image.download_url)}' only",
                "stage-add": f"would copy + install add {target_str}",
                "predownload": (
                    f"would copy + add {target_str}, then predownload to "
                    f"{len(roster)} joined AP(s) and STOP"
                ),
                "full": (
                    f"would copy + add + predownload to {len(roster)} AP(s), then "
                    f"ACTIVATE (controller reload; every joined AP reboots), commit"
                ),
            }[run_scope]
            clean_note = " Would FIRST clean the device (remove inactive)." if clean_before else ""
            return f"DRY-RUN ok:{clean_note} {plan}. All pre-flight gates passed."

        # -- 4. Transfer (same tiers as the switch job) ------------------------
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
                        "xcopy ended in a device-reported terminal failure — falling "
                        "back to classic copy. (%s)",
                        exc,
                        extra=log,
                    )
                    self._copy_image(client, image, log, target_fs)
        else:
            self._copy_image(client, image, log, target_fs)
        if run_scope == "stage-copy":
            return f"STAGED (copy): '{image.image_file_name}' is on {target_fs} and size-verified."

        # -- 5. install add ----------------------------------------------------
        ledger_confirmed_add = self._install_add(
            client, image, str(uuid_lib.uuid4()), log, target_fs
        )
        # The AP-side identity of the target is LEARNED from the device, not
        # derived from the version string: rebuild letters (17.15.4d) have no
        # numeric form, and even a plain string says nothing about the AP
        # image build field. Post-add, prepare-location publishes the staged
        # bundle's exact quad (prepare-minus-active set difference).
        target_quad = self._learn_target_quad(client, log)
        self._model_map_advisory(client, roster, target_quad, allow_unsupported_aps, run_scope, log)
        if run_scope == "stage-add":
            return f"STAGED (add): {target_str} is marked for activation."
        if target_quad is None:
            raise UpgradeAbort(
                "Could not learn the staged bundle's AP image version from "
                "prepare-location (see the log for what the device published) — "
                "per-AP predownload completion cannot be positively confirmed "
                "without it. Staging is complete; refusing the predownload/full "
                "scopes rather than guessing."
            )

        # -- 6. AP predownload (the point of this job) -------------------------
        predownload_summary, r0_fire = self._predownload_fleet(
            client,
            target_quad,
            deadline_minutes,
            tolerate_missing_aps,
            allow_unsupported_aps,
            run_scope,
            log,
        )
        if run_scope == "predownload":
            return f"PREDOWNLOADED: {predownload_summary} Window run needs only Full."

        # -- 7. Activate -> reload -> commit (controller-side facts only) ------
        health_pre = None
        if health_checks:
            try:
                health_pre = self._capture_wlc_health(client)
            except RestconfError as exc:
                raise UpgradeAbort(
                    f"Health checks were requested but the baseline failed ({exc}) — "
                    "aborting before activation (a requested baseline must exist)."
                ) from exc
            self._attach_health_artifact(f"health-pre_{device.name}.json", health_pre, log)
            self.logger.info(
                "Health baseline: %d AP(s), %d radio(s) up, last reboot '%s'.",
                len(health_pre["aps"]),
                sum(1 for s in health_pre["radios"].values() if s == ("enabled", "radio-up")),
                health_pre["reboot"].get("reason") or "unreported",
                extra=log,
            )
        if save_config:
            self._save_config(client, log)
        self._wait_for_engine_idle(
            client, log, "install activate", settle_fallback=not ledger_confirmed_add
        )
        act_uuid = str(uuid_lib.uuid4())
        resend = self._install_activate(client, image, act_uuid, log)
        self._confirm_activation(client, target_str, act_uuid, log, resend)
        self._wait_for_target(client, target_str, log)
        self._log_rollback_state(client, log)
        try:
            committed = self._install_commit(client, target_str, log)
        except Exception as exc:  # noqa: BLE001 - real rollback risk if commit fails
            raise UpgradeAbort(
                f"Controller booted {target_str} but COMMIT failed ({exc}). It is "
                "ACTIVATED but NOT committed — re-run this job (it will commit) "
                "before the auto-abort timer expires, or every predownloaded AP "
                "reverts with the controller (a second fleet-wide outage)."
            ) from exc
        sync_note = ""
        try:
            self._sync_nautobot(device, target_version, log)
        except Exception as exc:  # noqa: BLE001 - device committed; Nautobot lagged
            self.logger.error(
                "Committed, but the Nautobot software_version update failed (%s).",
                exc,
                extra=log,
            )
            sync_note = " (Nautobot software_version update FAILED — set it manually)"

        # -- 8. Post-commit tail: report-only by design ------------------------
        # AP rejoin NEVER gates the commit: refusing to commit on an AP
        # shortfall lets the auto-abort timer revert the controller, forcing
        # every AP that DID swap to downgrade again — a guaranteed second
        # fleet-wide outage traded for a partial one. Do not "fix" this.
        # Rejoin is measured against the FIRE-TIME roster (review finding —
        # the step-3 roster can be hours stale by the window).
        rejoin_note = self._rejoin_report(client, r0_fire or roster, target_quad, log)
        if save_config_after and committed:
            try:
                self._save_config(client, log, context="after commit")
            except UpgradeAbort as exc:
                raise UpgradeAbort(
                    f"The upgrade IS committed{sync_note}, but the post-commit save failed: {exc}"
                ) from exc
        if remove_inactive and committed:
            self._remove_inactive(client, log)
        if health_checks and health_pre is not None and committed:
            try:
                self._post_wlc_health_check(client, device, health_pre, log)
            except JobStopped:
                self.logger.warning(
                    "Stop requested during the post-upgrade health check — check cut "
                    "short. The upgrade IS committed and synced.",
                    extra=log,
                )
            except RestconfError as exc:
                # Report-only by decision: a health capture failure must never
                # re-classify a committed, synced upgrade as a failed device.
                self.logger.warning(
                    "Post-upgrade health check could not be captured (%s) — the "
                    "commit stands; compare against the health-pre artifact "
                    "manually.",
                    exc,
                    extra=log,
                )
        if not committed:
            # The one device fact the summary must never overstate (review
            # finding): commit was ISSUED but not positively confirmed.
            return (
                f"ACTIVATED to {target_str}; commit issued but NOT positively "
                f"confirmed — verify with 'show install summary' before the "
                f"auto-abort window closes{sync_note}. {predownload_summary} "
                f"{rejoin_note}"
            )
        return (
            f"SUCCESS: {target_str} committed on the controller{sync_note}. "
            f"{predownload_summary} {rejoin_note}"
        )

    # ------------------------------------------------------ wireless phases --

    def _read_roster(self, client, log):
        """Joined-AP roster, or raise: unreadable is NOT an empty fleet.

        A positively-read empty list ({} from a 204) is a REAL answer — a
        controller with no APs (the backup in a primary/backup design) — and
        returns {}. A transport/HTTP failure raises instead: silence must
        never look like 'zero APs' when a reload is on the table.
        """
        payload = client.get(C.DATA_AP_CAPWAP, ok_404=True)
        if payload is None:
            # 404 = the wireless model itself is absent — not a controller?
            raise UpgradeAbort(
                "capwap-data is not published (HTTP 404) — this device does not "
                "look like a wireless controller (or the wireless oper models are "
                "unavailable). Refusing."
            )
        return _ap_roster(payload)

    def _blast_radius_echo(self, client, roster, log):
        """What '1 device' means HERE — logged at run start and in dry-run."""
        if not roster:
            self.logger.info(
                "Blast-radius echo: NO APs joined (positively read). Normal for a "
                "backup controller — predownload will have nothing to do and a "
                "Full run reloads only the controller itself.",
                extra=log,
            )
            return
        models = Counter(_strip_reg_suffix(ap["model"]) for ap in roster.values())
        with_backup = 0
        primed = 0
        try:
            oper = client.get(C.DATA_AP_OPER_DATA, ok_404=True) or {}
        except RestconfError as exc:
            # Purely informational read — a blip here must never fail a run
            # (review finding); the echo says so instead.
            self.logger.warning("Prime-info read failed (%s) — coverage unknown.", exc, extra=log)
            oper = {}
        for entry in _oper_entries(oper, "oper-data"):
            prime = entry.get("ap-prime-info") or {}
            if not prime:
                continue
            primed += 1
            if prime.get("secondary-controller-name"):
                with_backup += 1
        self.logger.warning(
            "Blast-radius echo: **%d AP(s) joined** (%s). A Full run reboots every "
            "one of them. Backup-controller coverage (device-published prime info): "
            "%d of %d APs have a secondary controller configured%s.",
            len(roster),
            ", ".join(f"{m} x{n}" for m, n in sorted(models.items())),
            with_backup,
            primed if primed else len(roster),
            "" if primed else " (prime info not readable — coverage unknown)",
            extra=log,
        )

    def _learn_target_quad(self, client, log):
        """The staged bundle's AP image quad, learned from the device post-add.

        Reads prepare-location and active-location and takes the set
        difference (see _learned_target_quad). Returns None — with the
        device's actual publication logged — when the answer is not exactly
        one quad; callers fail closed on None for the predownload scopes.
        Blip doctrine (review finding): the learn happens once, potentially
        hours into a run — a single transient read failure must not convert
        into the None-refusal downstream. Bounded retries, then honest None.
        """
        prepare = active = None
        for attempt in range(3):
            try:
                prepare = client.get(C.DATA_AP_IMG_PREPARE, ok_404=True) or {}
                active = client.get(C.DATA_AP_IMG_ACTIVE, ok_404=True) or {}
                break
            except RestconfError as exc:
                self.logger.warning(
                    "AP image location read failed (attempt %d/3: %s)%s.",
                    attempt + 1,
                    exc,
                    " — retrying" if attempt < 2 else " — no learned quad",
                    extra=log,
                )
                if attempt == 2:
                    return None
                time.sleep(20)
        quad, detail = _learned_target_quad(prepare, active)
        if quad:
            self.logger.info(
                "Learned the staged bundle's AP image version from the device: **%s** (%s).",
                ".".join(map(str, quad)),
                detail,
                extra=log,
            )
        else:
            self.logger.warning("No unambiguous staged AP image quad: %s.", detail, extra=log)
        return quad

    def _model_map_advisory(self, client, roster, target_quad, allow_unsupported, run_scope, log):
        """Device-published 'is every joined model covered?' check, post-add.

        prepare-location carries the TARGET bundle's model map only after
        install add (bench-proven), selected by the LEARNED quad. Missing
        models refuse the predownload scopes unless allow_unsupported_aps —
        an uncovered AP takes the slow post-reload path or cannot join the
        new release at all.
        """
        if not roster or target_quad is None:
            return
        try:
            payload = client.get(C.DATA_AP_IMG_PREPARE, ok_404=True) or {}
        except RestconfError as exc:
            # Advisory read: refusal is reserved for a POSITIVE not-covered
            # reading — an unreadable map is unknown coverage, said out loud
            # (the per-AP predownload gate still protects the reload).
            self.logger.warning(
                "Model-map advisory: prepare-location unreadable (%s) — coverage unknown.",
                exc,
                extra=log,
            )
            return
        covered = _model_map(payload, target_quad)
        if not covered:
            self.logger.warning(
                "Model-map advisory: prepare-location published no image map for "
                "the target release — cannot pre-check model coverage (the "
                "per-AP predownload gate still protects the reload).",
                extra=log,
            )
            return
        missing = {}
        for ap in roster.values():
            base = _strip_reg_suffix(ap["model"])
            if base and base not in covered:
                missing.setdefault(base, []).append(ap["name"])
        if not missing:
            self.logger.info(
                "Model-map advisory: every joined AP model is covered by the "
                "target bundle (%d image(s), device-published).",
                len(set(covered.values())),
                extra=log,
            )
            return
        detail = "; ".join(
            f"{model}: {', '.join(sorted(names))}" for model, names in sorted(missing.items())
        )
        if allow_unsupported and run_scope in ("predownload", "full"):
            self.logger.warning(
                "Model-map advisory: models NOT covered by the target bundle — %s. "
                "Proceeding because 'Allow predownload-unsupported AP models' is "
                "ticked; these APs take the slow path or cannot run the target.",
                detail,
                extra=log,
            )
        elif run_scope in ("predownload", "full"):
            raise UpgradeAbort(
                f"The target bundle's device-published model map does not cover: "
                f"{detail}. Refusing the predownload scopes (fail closed). Tick "
                "'Allow predownload-unsupported AP models' to proceed anyway."
            )
        else:
            self.logger.warning(
                "Model-map advisory (staging only): models NOT covered by the target bundle — %s.",
                detail,
                extra=log,
            )

    def _predownload_fleet(
        self,
        client,
        target_quad,
        deadline_minutes,
        tolerate_missing_aps,
        allow_unsupported,
        run_scope,
        log,
    ):
        """Fire predownload and hold the R0 contract to completion.

        Returns (summary, r0) on success — r0 is the fire-time roster the
        post-commit rejoin report measures against. Raises UpgradeAbort when
        the deadline expires with unsatisfied APs — unless scope is 'full'
        AND tolerate_missing_aps, where it proceeds with every straggler
        named.
        """
        # R0: the contract. Captured fresh, immediately before firing.
        r0 = self._read_roster(client, log)
        if not r0:
            self.logger.info(
                "Predownload: no APs joined (positively read) — nothing to "
                "predownload. Proceeding.",
                extra=log,
            )
            return "No APs were joined; predownload had nothing to do.", {}
        if target_quad is None:
            # Callers gate on this already; kept as a hard invariant so no
            # refactor can reach the fire without a learned quad.
            raise UpgradeAbort(
                "No learned AP image quad for the staged bundle — per-AP "
                "completion could not be positively confirmed. Refusing "
                "before the fire."
            )

        pred_before = _predownload_states(client.get(C.DATA_AP_PREDOWNLOAD, ok_404=True) or {})
        report = _completion_report(r0, pred_before, r0, target_quad, allow_unsupported)
        if all(ok for ok, _, _ in report.values()):
            # Idempotent re-run: the fleet already holds the target (stale
            # complete entries and/or backup partitions at the target quad).
            # Firing again is a documented no-op; skip it for a quiet log.
            # Waived-unsupported APs are NAMED here too — this branch can
            # pass Full's gate, so its accounting must not overstate.
            counts = _report_counts(report)
            waived = _named(report, r0, wanted_states={"unsupported"})
            if waived:
                self.logger.warning(
                    "Predownload skip: %d AP(s) counted ONLY because "
                    "'Allow predownload-unsupported' is ticked (slow path after "
                    "the reload): %s.",
                    len(waived),
                    "; ".join(waived),
                    extra=log,
                )
            summary = (
                f"Predownload skipped — {len(r0)} AP(s) already satisfied "
                f"({', '.join(f'{s}: {n}' for s, n in sorted(counts.items()))})."
            )
            return summary, r0
        if pred_before:
            stale = [f"{mac}: {st}" for mac, (st, _q) in sorted(pred_before.items())]
            self.logger.info(
                "Predownload list before firing (stale entries are normal): %s.",
                "; ".join(stale),
                extra=log,
            )

        deadline = time.monotonic() + max(5, int(deadline_minutes or 120)) * 60
        fired = self._fire_predownload(client, log)
        engage_deadline = time.monotonic() + C.PREDOWNLOAD_ENGAGE_SECS
        resent = False
        last_counts = None
        blip_streak = 0
        self.logger.info(
            "Watching %d AP(s) to completion (deadline %s min; poll %ss). The "
            "deadline only ever declares failure.",
            len(r0),
            deadline_minutes,
            C.PREDOWNLOAD_POLL_SECS,
            extra=log,
        )
        while True:
            self._check_stop()
            time.sleep(C.PREDOWNLOAD_POLL_SECS)
            # A transient read failure is an UNREADABLE POLL, never evidence
            # (review finding; same blip class _await_op tolerates via
            # LEDGER_BLIP_POLLS). Only the deadline condemns.
            try:
                pred_payload = client.get(C.DATA_AP_PREDOWNLOAD, ok_404=True)
                roster_payload = client.get(C.DATA_AP_CAPWAP, ok_404=True)
            except RestconfError as exc:
                blip_streak += 1
                self.logger.warning(
                    "Predownload watch: unreadable poll (%s) — retrying "
                    "(streak: %d; an unreadable poll is never evidence).",
                    exc,
                    blip_streak,
                    extra=log,
                )
                if time.monotonic() > deadline:
                    raise UpgradeAbort(
                        "Predownload deadline expired while the device was "
                        "unreadable — completion could not be positively confirmed."
                    ) from exc
                continue
            blip_streak = 0
            pred = _predownload_states(pred_payload or {})
            roster_now = _ap_roster(roster_payload or {})
            report = _completion_report(r0, pred, roster_now, target_quad, allow_unsupported)
            counts = _report_counts(report)
            if counts != last_counts:
                last_counts = counts
                self.logger.info(
                    "Predownload progress: %s.",
                    ", ".join(f"{state}: {n}" for state, n in sorted(counts.items())),
                    extra=log,
                )
            if all(ok for ok, _, _ in report.values()):
                summary = (
                    f"{len(r0)} AP(s) hold the target "
                    f"({', '.join(f'{s}: {n}' for s, n in sorted(counts.items()))})."
                )
                accepted_unsupported = _named(report, r0, wanted_states={"unsupported"})
                if accepted_unsupported:
                    self.logger.warning(
                        "Predownload complete WITH accepted unsupported AP(s): %s.",
                        "; ".join(accepted_unsupported),
                        extra=log,
                    )
                self.logger.info("Predownload complete: %s", summary, extra=log)
                return summary, r0
            # Engagement = a CHANGE vs the pre-fire snapshot for an
            # UNSATISFIED roster AP (review finding: stale entries and
            # already-held APs must not mask a lost fire).
            engaged = any(
                mac in pred and pred.get(mac) != pred_before.get(mac)
                for mac, (ok, _s, _w) in report.items()
                if not ok
            )
            if not engaged and time.monotonic() > engage_deadline:
                if not resent:
                    # One bounded resend, same pattern as activation's
                    # engine-drops-the-request handling. WNCD batching makes
                    # slow engagement NORMAL; a silent lost fire is not.
                    self.logger.warning(
                        "No unsatisfied AP has engaged within %ss — resending the "
                        "predownload fire once (fresh uuid).",
                        C.PREDOWNLOAD_ENGAGE_SECS,
                        extra=log,
                    )
                    fired = self._fire_predownload(client, log)
                    resent = True
                    engage_deadline = time.monotonic() + C.PREDOWNLOAD_ENGAGE_SECS
                else:
                    raise UpgradeAbort(
                        "AP predownload never engaged (no unsatisfied roster AP's "
                        "predownload entry changed after the fire and one resend). "
                        f"The fire uuid was {fired} — not echoed anywhere by "
                        "design; check controller logs."
                    )
            if time.monotonic() > deadline:
                incomplete = _named(report, r0, satisfied=False)
                if run_scope == "full" and tolerate_missing_aps:
                    self.logger.warning(
                        "Predownload deadline expired with %d AP(s) NOT holding the "
                        "target — proceeding because 'Proceed despite incomplete "
                        "APs' is ticked. These APs take the SLOW post-reload path: "
                        "%s.",
                        len(incomplete),
                        "; ".join(incomplete),
                        extra=log,
                    )
                    return (
                        f"{len(r0) - len(incomplete)} of {len(r0)} AP(s) hold the "
                        f"target; PROCEEDED past {len(incomplete)} incomplete "
                        f"(named in the log) by operator choice.",
                        r0,
                    )
                raise UpgradeAbort(
                    f"Predownload deadline ({deadline_minutes} min) expired with "
                    f"{len(incomplete)} of {len(r0)} AP(s) not holding the target: "
                    f"{'; '.join(incomplete)}. Nothing was activated. Re-run to "
                    "continue (completed APs are skipped), extend the deadline, or "
                    "tick 'Proceed despite incomplete APs' for a Full run."
                )

    def _fire_predownload(self, client, log):
        """POST set-rad-predownload-all; returns the fired uuid.

        Accepted = 2xx (204-empty per RFC 8040, bench-confirmed). A refusal
        is an HTTP 4xx and raises via the client — positively terminal
        (bench: refusals are structured errors with useless message text;
        the status class is the signal).
        """
        op_uuid = str(uuid_lib.uuid4())
        payload = {"Cisco-IOS-XE-wireless-access-point-cmd-rpc:input": {"uuid": op_uuid}}
        try:
            client.post_rpc(C.OP_AP_PREDOWNLOAD_ALL, payload, timeout=C.RPC_TIMEOUT)
        except RestconfError as exc:
            if exc.status_code is None:
                # Transport failure: the RPC may have been accepted and be
                # running fleet-wide — 'refused' would assert state the device
                # never published (review finding).
                raise UpgradeAbort(
                    f"The predownload fire's outcome is UNKNOWN (transport "
                    f"failure: {exc}). Nothing was activated; the device may "
                    "still be predownloading. Re-running is safe — completed "
                    "APs are skipped."
                ) from exc
            raise UpgradeAbort(
                f"The predownload fire was REFUSED by the controller ({exc}). "
                "Common causes: no staged image for the APs, or the wireless "
                "process is busy. Nothing was activated."
            ) from exc
        self.logger.info(
            "AP predownload fired (uuid %s — not echoed in oper data by design; "
            "tracking is the roster-contract state join).",
            op_uuid,
            extra=log,
        )
        return op_uuid

    def _rejoin_report(self, client, r0, target_quad, log):
        """Post-commit, report-only AP rejoin watch. Facts, never verdicts.

        Bounded by REJOIN_REPORT_SECS; ends early when every R0 AP is back
        registered on the target. An AP in 'downloading' is the slow-path
        signature (bench: empty version fields while it fetches). A missing
        AP may be parked on its backup controller, which this job does not
        query — the wording says so.
        """
        if not r0:
            return "No APs to watch rejoin."
        report_deadline = time.monotonic() + C.REJOIN_REPORT_SECS
        last_line = ""
        try:
            while True:
                # An unreadable poll must not RENDER as '0 rejoined' — silence
                # is never report evidence (review finding): skip evaluation
                # and let the last successful line stand.
                try:
                    roster_now = _ap_roster(client.get(C.DATA_AP_CAPWAP, ok_404=True) or {})
                except RestconfError as exc:
                    self.logger.warning(
                        "AP rejoin: unreadable poll (%s) — not evidence; retrying.",
                        exc,
                        extra=log,
                    )
                    if time.monotonic() > report_deadline:
                        return (
                            f"AP rejoin (report window ended on an unreadable poll): "
                            f"last successful reading was: {last_line or 'none'}. "
                            "Rejoin is report-only by design — the commit stands."
                        )
                    self._check_stop()
                    time.sleep(C.PREDOWNLOAD_POLL_SECS)
                    continue
                # The AP's post-swap sw-version IS the AP image quad
                # (bench: 17.18.3.18) — compare exactly against the learned
                # quad; with none learned, count registered-only and say so.
                quad_str = ".".join(map(str, target_quad)) if target_quad else None
                back = {
                    mac
                    for mac, ap in roster_now.items()
                    if mac in r0
                    and ap["oper"] == "registered"
                    and (quad_str is None or str(ap.get("sw", "")) == quad_str)
                }
                downloading = [
                    r0.get(mac, roster_now[mac])["name"]
                    for mac, ap in roster_now.items()
                    if mac in r0 and ap["oper"] == "downloading"
                ]
                missing = [r0[mac]["name"] for mac in r0 if mac not in roster_now]
                line = (
                    f"{len(back)} of {len(r0)} AP(s) rejoined on the target"
                    + ("" if quad_str else " (registered-only; no learned quad to compare)")
                    + (
                        f"; downloading (slow path): {', '.join(sorted(downloading))}"
                        if downloading
                        else ""
                    )
                    + (
                        f"; not yet joined: {', '.join(sorted(missing))} (may be on their "
                        "backup controller, which this job does not query)"
                        if missing
                        else ""
                    )
                )
                if line != last_line:
                    self.logger.info("AP rejoin: %s.", line, extra=log)
                    last_line = line
                if len(back) == len(r0):
                    return f"AP rejoin: all {len(r0)} AP(s) back on the target."
                if time.monotonic() > report_deadline:
                    return (
                        f"AP rejoin (report window ended): {line}. Rejoin is "
                        "report-only by design — the commit stands."
                    )
                self._check_stop()
                time.sleep(C.PREDOWNLOAD_POLL_SECS)
        except JobStopped:
            self.logger.warning(
                "Stop requested during the rejoin report — report cut short; the commit stands.",
                extra=log,
            )
            return f"AP rejoin (cut short by stop): {last_line or 'no polls completed'}."

    # ---------------------------------------------------- wireless health ----

    def _capture_wlc_health(self, client):
        """Wireless health snapshot (fail-closed: caller aborts on failure).

        aps: {mac: {name, sw, oper}}; radios: {(mac, slot): (admin, oper)};
        reboot: reason + severity; env: sensor states (auto-skip when empty).
        """
        roster = _ap_roster(client.get(C.DATA_AP_CAPWAP, ok_404=False) or {})
        radios = {}
        radio_payload = client.get(C.DATA_AP_RADIO_OPER, ok_404=True) or {}
        for entry in _oper_entries(radio_payload, "radio-oper-data"):
            mac = str(entry.get("wtp-mac", "")).lower()
            slot = entry.get("radio-slot-id")
            if mac and slot is not None:
                radios[f"{mac}/{slot}"] = (
                    str(entry.get("admin-state", "")),
                    str(entry.get("oper-state", "")),
                )
        system = (client.get(C.DATA_DEVICE_SYSTEM, ok_404=False) or {}).get(
            "Cisco-IOS-XE-device-hardware-oper:device-system-data", {}
        )
        env = _parse_env(client.get(C.DATA_ENV_SENSORS, ok_404=True) or {})
        return {
            "aps": {
                mac: {"name": ap["name"], "sw": ap["sw"], "oper": ap["oper"]}
                for mac, ap in roster.items()
            },
            "radios": radios,
            "reboot": {
                "reason": system.get("last-reboot-reason"),
                "severity": system.get("reason-severity"),
            },
            "env": env,
        }

    def _post_wlc_health_check(self, client, device, pre, log):
        """Report-only comparison against the pre-activation baseline.

        The caller catches RestconfError and JobStopped — nothing raised here
        may re-classify the committed upgrade as failed.
        """
        self._check_stop()
        post = self._capture_wlc_health(client)
        self._attach_health_artifact(f"health-post_{device.name}.json", post, log)
        findings = []
        pre_aps, post_aps = pre["aps"], post["aps"]
        for mac in sorted(set(pre_aps) - set(post_aps)):
            findings.append(
                f"AP GONE: {pre_aps[mac]['name']} ({mac}) was joined before and is "
                "not now (may be on its backup controller)"
            )
        for mac in sorted(set(post_aps) & set(pre_aps)):
            ap = post_aps[mac]
            if ap["oper"] == "downloading":
                findings.append(
                    f"AP SLOW PATH: {ap['name']} is downloading its image post-reload "
                    "(predownload did not deliver for it)"
                )
            elif ap["oper"] != "registered":
                findings.append(f"AP NOT REGISTERED: {ap['name']} is '{ap['oper']}'")
            elif pre_aps[mac]["sw"] and ap["sw"] == pre_aps[mac]["sw"]:
                findings.append(f"AP OLD IMAGE: {ap['name']} rejoined still on {ap['sw']}")
        for key, (admin, oper) in sorted(pre["radios"].items()):
            if admin == "enabled" and oper == "radio-up":
                now = post["radios"].get(key)
                if now is None or now != ("enabled", "radio-up"):
                    findings.append(
                        f"RADIO DOWN: {key} was enabled/up before, now {now or 'absent'}"
                    )
        sev = post["reboot"].get("severity")
        if sev and sev != "normal":
            findings.append(f"RELOAD REASON: severity '{sev}' ({post['reboot'].get('reason')})")
        pre_env_ok = {k for k, s in pre["env"].items() if _env_ok(s)}
        for sensor in sorted(pre_env_ok):
            if not _env_ok(post["env"].get(sensor, "")):
                findings.append(f"ENV: sensor '{sensor}' degraded since the baseline")
        new_aps = sorted(set(post_aps) - set(pre_aps))
        if new_aps:
            self.logger.info(
                "Health note (never a finding): %d new AP(s) joined since the baseline: %s.",
                len(new_aps),
                ", ".join(post_aps[m]["name"] for m in new_aps),
                extra=log,
            )
        if findings:
            for f in findings:
                self.logger.warning("Health finding (report-only): %s.", f, extra=log)
            self.logger.warning(
                "%d wireless health finding(s) — report-only by design; the upgrade "
                "is committed. Artifacts: health-pre/health-post.",
                len(findings),
                extra=log,
            )
        else:
            self.logger.info("Wireless health checks: no regressions found.", extra=log)
