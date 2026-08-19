"""RESTCONF Dev Tester — a recording instrument, not an automation.

This job points at ONE device and captures raw RESTCONF evidence as JobResult
file artifacts: exact response bodies, HTTP statuses, and read timings. It
exists so a tester can gather bench evidence by running a job instead of
hand-driving Postman, and so field diagnostics can be collected the same way
("run the Dev Tester, send me the Job Result").

Design rule (deliberate, do not "improve"): the job RECORDS and never DECIDES.
GET suites are strictly read-only. POST suites fire exactly ONE
operator-selected RPC, record the request/response/after-state, and stop — no
retries (beyond the documented bodiless->{} shape fallback for no-input RPCs),
no chaining, no interpretation. Anything smarter belongs in a purpose-built
job whose behavior has been bench-proven first.

The current probe packs target the Catalyst 9800 AP-predownload bench (see the
bench sheet delivered 2026-08-02); the construct is general.
"""

import json
import re
import time
import uuid as uuid_lib
from datetime import datetime, timezone

from celery.exceptions import SoftTimeLimitExceeded
from nautobot.apps.jobs import (
    BooleanVar,
    ChoiceVar,
    IntegerVar,
    Job,
    ObjectVar,
    StringVar,
    TextVar,
)
from nautobot.dcim.models import Device
from nautobot.extras.choices import SecretsGroupAccessTypeChoices, SecretsGroupSecretTypeChoices
from nautobot.extras.models import SecretsGroup
from nautobot.extras.models.secrets import SecretsGroupAssociation

from . import constants as C
from .restconf import RestconfClient

name = "IOS-XE Upgrades"


class DevTesterAbort(Exception):
    """Fatal input/setup problem — fail loudly before touching the device."""


# --- probe packs --------------------------------------------------------------

#: Artifact names deliberately match the bench sheet's P-numbers so the sheet's
#: results log stays the shared vocabulary between manual and job-driven runs.
AP_OPER = "data/Cisco-IOS-XE-wireless-access-point-oper:access-point-oper-data"
SNAPSHOT_PROBES = (
    ("P0-system", C.DATA_DEVICE_SYSTEM),
    ("P1-install-oper", C.DATA_INSTALL_OPER),
    ("P2-stack-oper", "data/Cisco-IOS-XE-stack-oper:stack-oper-data"),
    ("P2c-inventory", C.DATA_DEVICE_INVENTORY),
    ("P3-predownload", f"{AP_OPER}/predownload-data"),
    ("P4-capwap", f"{AP_OPER}/capwap-data"),
    ("P6-ap-global", "data/Cisco-IOS-XE-wireless-ap-global-oper:ap-global-oper-data"),
    ("P7-q-filesystem", C.DATA_Q_FILESYSTEM),
)
WATCH_PROBES = (
    ("predownload", f"{AP_OPER}/predownload-data"),
    ("capwap", f"{AP_OPER}/capwap-data"),
)

AP_CMD_RPC = "operations/Cisco-IOS-XE-wireless-access-point-cmd-rpc"
#: POST suites: operation path + whether the RPC takes a uuid input (per the
#: published YANG: set-rad-predownload-all and ap-image-predownload-abort have
#: a mandatory uuid; dry-run and clear-statistics take NO input).
POST_SUITES = {
    "post-dryrun": (f"{AP_CMD_RPC}:ap-image-upgrade-dry-run", False),
    "post-fire": (f"{AP_CMD_RPC}:set-rad-predownload-all", True),
    "post-abort": (f"{AP_CMD_RPC}:ap-image-predownload-abort", True),
    "post-clear": (f"{AP_CMD_RPC}:clear-ap-predownload-statistics", False),
}

SUITE_CHOICES = (
    ("get-snapshot", "GETs — snapshot + analyses (read-only)"),
    ("get-watch", "GETs — watch recorder (read-only, polls until duration ends)"),
    ("post-dryrun", "POST — ap-image-upgrade-dry-run (sends an RPC; no documented input)"),
    ("post-fire", "POST — FIRE AP predownload to ALL joined APs (sends a command!)"),
    ("post-abort", "POST — abort AP predownload (sends a command!)"),
    ("post-clear", "POST — clear AP predownload statistics (sends a command!)"),
)

#: Watch mode ends early after this many consecutive polls where NEITHER
#: watched resource changed (a recorder note, not a verdict about the device).
WATCH_STEADY_POLLS = 5
#: Cap each artifact; a truncated artifact says so loudly at the point of cut.
MAX_ARTIFACT_BYTES = 3_000_000
#: Aggregate cap across a whole run — past this the run degrades to a
#: manifest-only ledger instead of exhausting JobResult storage.
TOTAL_ARTIFACT_BUDGET = 150_000_000
#: Delay between a POST and its after-state read: long enough for the
#: controller to register the command, short enough to catch early state.
POST_AFTER_DELAY_SECS = 5

_PRED_STATUS_RE = re.compile(r'"pred-status"\s*:\s*"([\w-]+)"')
_WTP_MAC_RE = re.compile(r'"wtp-mac"\s*:\s*"')
_BOOT_MODE_RE = re.compile(r'"boot-mode"\s*:\s*"([\w-]+)"')


def _iter_key(obj, key):
    """Yield every value of ``key`` anywhere in a parsed-JSON structure.

    Iterative (explicit stack): recursive generators hit the recursion limit
    on deep payloads long before json.loads does, and an advisory analysis
    must never be able to crash the recording.
    """
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if k == key:
                    yield v
                stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)


def _parse_or_none(text):
    try:
        return json.loads(text) if text else None
    except (ValueError, RecursionError):
        return None


def _artifact_content(record):
    """Artifact body for a probe record: raw device bytes, or a loud error stub.

    A transport failure must never produce a zero-byte artifact — an empty
    'before' file next to a populated 'after' file would read as evidence the
    RPC created entries, when the before read simply failed.
    """
    return record["text"] or json.dumps(
        {"status": record["status"], "error": record["error"], "note": "no response body"}
    )


def _utc_now():
    """UTC ISO-8601 wall-clock — every manifest row carries one, so the bundle
    can answer 'when was poll N' without the Nautobot UI (the bench sheet's
    fire-time/done-time questions live or die on these)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _readable(record):
    """True when the probe was a genuine 2xx read — the only kind that may
    participate in steady-state accounting."""
    return record["status"] is not None and 200 <= record["status"] < 300


_CUSTOM_PATH_OK = re.compile(r"^data/[\w.:/,;()=%&?+@\[\]-]+$")


def _custom_path_reason(line):
    """Reason a custom GET path is refused, else None.

    Positive validation, not just a prefix check: 'data/../operations/...'
    starts with 'data/' but lands on an RPC after server-side normalization.
    Read-only must be enforced by the validator, not by luck of the verb.
    """
    if ".." in line:
        return "dot segments are not allowed"
    if "://" in line or line.startswith("/") or "\\" in line:
        return "must be a relative RESTCONF path under data/"
    if not _CUSTOM_PATH_OK.match(line):
        return "only read-only 'data/...' paths with RESTCONF characters are allowed"
    return None


def _record_line(label, record):
    status = record["status"] if record["status"] is not None else "TRANSPORT-ERROR"
    line = f"{label}: HTTP {status} in {record['elapsed_ms']} ms, {len(record['text'])} bytes"
    if record["error"]:
        line += f" — {record['error']}"
    return line


class RestconfDevTester(Job):
    """Record raw RESTCONF evidence from one device as Job Result artifacts."""

    device = ObjectVar(
        model=Device,
        description="The ONE device to probe. POST suites send commands to this device.",
    )
    secrets_group_override = ObjectVar(
        model=SecretsGroup,
        required=False,
        description="Override the device's own Secrets Group for RESTCONF credentials.",
    )
    test_suite = ChoiceVar(
        choices=SUITE_CHOICES,
        default="get-snapshot",
        description=(
            "GET suites only read. POST suites SEND A COMMAND to the device and "
            "require the acknowledgement box below."
        ),
    )
    custom_get_paths = TextVar(
        required=False,
        label="Custom GET paths (snapshot suite only)",
        description=(
            "Optional extra read-only probes, one RESTCONF path per line, each "
            "starting with 'data/'. Anything else is refused."
        ),
    )
    hunt_string = StringVar(
        required=False,
        description=(
            "Optional: search every captured payload for this string (e.g. a fired "
            "operation uuid) and report where it appears."
        ),
    )
    watch_interval_seconds = IntegerVar(
        default=60,
        min_value=15,
        max_value=600,
        description="Watch suite: seconds between polls.",
    )
    watch_duration_minutes = IntegerVar(
        default=30,
        min_value=1,
        max_value=240,
        description=(
            "Watch suite: total watch window. Must fit inside the worker's job time "
            "limits — the watch also ends early once the data stops changing."
        ),
    )
    confirm_writes = BooleanVar(
        default=False,
        label="I understand this suite SENDS COMMANDS to the device",
        description=(
            "Required for POST suites. Firing AP predownload makes every joined AP "
            "download an image in the background; do not point this at a production "
            "controller casually."
        ),
    )

    class Meta:
        name = "RESTCONF Dev Tester"
        description = (
            "Evidence recorder: capture raw RESTCONF responses, statuses, and timings "
            "from one device as Job Result file artifacts. Read-only GET suites plus "
            "explicitly-acknowledged single-RPC POST suites. Records, never decides. "
            "NOT covered here: image staging and activation (bench sheet P10/P15) are "
            "driven by the IOS-XE upgrade job or Postman, not by this recorder."
        )
        has_sensitive_variables = False
        # Sized for the maximum watch window (240 min) plus per-poll probe time
        # and slack. Without these, stock Nautobot's CELERY_TASK_SOFT_TIME_LIMIT
        # (300s) soft-kills even the DEFAULT 30-minute watch mid-sleep.
        soft_time_limit = 15000
        time_limit = 15600

    # ------------------------------------------------------------------ run --

    def run(
        self,
        device=None,
        secrets_group_override=None,
        test_suite="get-snapshot",
        custom_get_paths="",
        hunt_string="",
        watch_interval_seconds=60,
        watch_duration_minutes=30,
        confirm_writes=False,
    ):
        self.logger.info(
            "RESTCONF Dev Tester (nautobot-upgrades v%s) — suite: %s", C.JOB_VERSION, test_suite
        )
        valid = {value for value, _ in SUITE_CHOICES}
        if test_suite not in valid:
            # Stored kwargs from ScheduledJobs bypass form validation (hard-won
            # lesson from the transfer_method dropdown) — refuse loudly.
            raise DevTesterAbort(
                f"Unknown test_suite {test_suite!r} (stale ScheduledJob kwargs?). "
                f"Valid: {sorted(valid)}"
            )
        if device is None:
            raise DevTesterAbort("No device selected.")
        if test_suite in POST_SUITES and confirm_writes is not True:
            # Identity check, not truthiness: stored ScheduledJob/API kwargs
            # bypass form coercion, and the string "false" is truthy — the one
            # gate before a fleet-wide command must not pass on a stale string.
            raise DevTesterAbort(
                f"Suite '{test_suite}' SENDS A COMMAND to {device.name} and requires the "
                "'I understand this suite SENDS COMMANDS' acknowledgement "
                f"(received {confirm_writes!r}). Refusing."
            )

        log = {"object": device}
        client = RestconfClient(
            self._device_host(device),
            *self._credentials(device, secrets_group_override, log),
            logger=self.logger,
            log_object=device,
        )
        self._records = []
        self._attached_bytes = 0
        self._hunt = (hunt_string or "").strip().lower()
        self._hunt_hits = []
        self._hunt_searched = 0
        self._hunt_empty = 0

        try:
            if test_suite == "get-snapshot":
                self._run_snapshot(client, custom_get_paths or "", log)
            elif test_suite == "get-watch":
                self._run_watch(client, watch_interval_seconds, watch_duration_minutes, log)
            else:
                self._run_post(client, test_suite, log)
        except SoftTimeLimitExceeded:
            # Attach the index of everything captured so far before dying —
            # a killed watch must still yield its evidence ledger.
            self.logger.error(
                "Worker soft time limit hit mid-suite — attaching the manifest for "
                "the %s probe(s) already captured, then failing.",
                len(self._records),
                extra=log,
            )
            self._attach_manifest(test_suite, log)
            raise

        self._attach_manifest(test_suite, log)
        if self._hunt:
            scope = (
                f"{self._hunt_searched} payload(s) searched, {self._hunt_empty} empty/unreadable; "
                "only THIS suite's captures were searched — the install ledger (P1) and "
                "ap-global (P6) need a snapshot run with hunt_string set"
            )
            if self._hunt_hits:
                self.logger.warning(
                    "Hunt string %r FOUND in: %s (%s).",
                    self._hunt,
                    ", ".join(self._hunt_hits),
                    scope,
                    extra=log,
                )
            else:
                self.logger.info("Hunt string %r not found (%s).", self._hunt, scope, extra=log)
        return f"Suite '{test_suite}' captured {len(self._records)} probe responses as artifacts."

    # --------------------------------------------------------------- suites --

    def _run_snapshot(self, client, custom_get_paths, log):
        # Analyses read these LOCAL untruncated texts, never the (possibly
        # truncated) attached artifacts — a count from a cut capture would be
        # a silently-short lower bound presented as a total (review finding).
        parsed, texts = {}, {}
        for label, path in SNAPSHOT_PROBES:
            record = self._probe_get(client, label, path, log)
            texts[label] = record["text"]
            parsed[label] = _parse_or_none(record["text"])

        # P5: locate ap-prime-info. Only fetch the (large) full container when
        # the roster read didn't already carry it.
        if "ap-prime-info" in texts.get("P4-capwap", ""):
            self.logger.info("ap-prime-info found inside capwap-data (P4).", extra=log)
            parsed["P5-ap-oper-full"] = parsed.get("P4-capwap")
        else:
            self.logger.info(
                "ap-prime-info not in capwap-data — fetching the full AP oper container (P5).",
                extra=log,
            )
            record = self._probe_get(client, "P5-ap-oper-full", AP_OPER, log)
            parsed["P5-ap-oper-full"] = _parse_or_none(record["text"])

        accepted = 0
        for line in (p.strip() for p in custom_get_paths.splitlines() if p.strip()):
            reason = _custom_path_reason(line)
            if reason:
                self.logger.warning("Custom path %r refused: %s", line, reason, extra=log)
                # The manifest is the run's ledger — refused requests belong
                # in it too, so the bundle shows what was asked and declined.
                self._records.append(
                    {
                        "label": "CUSTOM-REFUSED",
                        "path": line,
                        "status": None,
                        "elapsed_ms": 0,
                        "bytes": 0,
                        "error": f"refused: {reason}",
                        "at": _utc_now(),
                    }
                )
                continue
            accepted += 1
            self._probe_get(client, f"CUSTOM-{accepted:02d}", line, log)

        try:
            self._snapshot_analyses(parsed, texts, log)
        except Exception as exc:  # noqa: BLE001 - advisories may never kill the recording
            self.logger.warning(
                "Snapshot analyses failed (%s) — advisory only; every raw capture is "
                "already attached.",
                exc,
                extra=log,
            )

    def _run_watch(self, client, interval, duration_minutes, log):
        self.logger.info(
            "Watch recorder: polling %s every %ss for up to %s min "
            "(ends early after %s unchanged polls).",
            " + ".join(label for label, _ in WATCH_PROBES),
            interval,
            duration_minutes,
            WATCH_STEADY_POLLS,
            extra=log,
        )
        deadline = time.monotonic() + duration_minutes * 60
        last_text = {}
        unchanged_streak = 0
        seq = 0
        # Two ledgers per resource, deliberately separate: last_attached drives
        # save-on-change for artifacts (error stubs included); last_body holds
        # the last GENUINE 2xx body and alone feeds steady-state accounting.
        # An unreachable device must never masquerade as a steady one — the
        # recorder would stop exactly when things get interesting, while its
        # log asserted the data stopped changing (review finding, HIGH).
        last_attached = {}
        while True:
            seq += 1
            changed_real = False
            all_readable = True
            for label, path in WATCH_PROBES:
                record = client.probe_get(path)
                now = _utc_now()
                self._note(f"watch-{label}-{seq:03d}", path, record)
                content = _artifact_content(record)
                if content != last_attached.get(label):
                    last_attached[label] = content
                    stamp = now.replace(":", "").replace("-", "")[:16]
                    self._attach(f"watch-{label}-{seq:03d}-{stamp}.json", content, log)
                if not _readable(record):
                    all_readable = False
                    self.logger.warning(
                        "poll %03d %s: no readable answer — %s (excluded from "
                        "steady-state accounting).",
                        seq,
                        label,
                        _record_line(label, record),
                        extra=log,
                    )
                    continue
                if record["text"] != last_text.get(label):
                    changed_real = True
                    last_text[label] = record["text"]
                    if label == "predownload":
                        self._log_pred_summary(seq, record, log)
                    else:
                        self.logger.info(
                            "poll %03d %s: %s (changed — saved)",
                            seq,
                            label,
                            _record_line(label, record),
                            extra=log,
                        )
            if changed_real:
                unchanged_streak = 0
            elif all_readable:
                unchanged_streak += 1
                self.logger.info("poll %03d: no change on either resource.", seq, extra=log)
            # else: some probe unreadable — the streak neither grows nor resets.
            if unchanged_streak >= WATCH_STEADY_POLLS:
                self.logger.info(
                    "Watch ended early: %s consecutive fully-readable unchanged polls "
                    "(a recorder observation, not a verdict about the device).",
                    unchanged_streak,
                    extra=log,
                )
                return
            if time.monotonic() + interval > deadline:
                self.logger.info("Watch window (%s min) exhausted.", duration_minutes, extra=log)
                return
            time.sleep(interval)

    def _run_post(self, client, suite, log):
        operation, wants_uuid = POST_SUITES[suite]

        before = client.probe_get(WATCH_PROBES[0][1])
        self._note(f"{suite}-before", WATCH_PROBES[0][1], before)
        self._attach(f"{suite}-before.json", _artifact_content(before), log)

        payload = None
        if wants_uuid:
            op_uuid = str(uuid_lib.uuid4())
            payload = {"Cisco-IOS-XE-wireless-access-point-cmd-rpc:input": {"uuid": op_uuid}}
            self.logger.warning(
                "Firing %s with uuid **%s** — record this uuid; it is the value to hunt "
                "for in later captures.",
                operation,
                op_uuid,
                extra=log,
            )
            if not self._hunt:
                self._hunt = op_uuid.lower()
        else:
            self.logger.info("Firing %s (no-input RPC, bodiless POST).", operation, extra=log)
        self._attach(
            f"{suite}-request.json",
            json.dumps({"operation": operation, "body": payload}),
            log,
        )

        record = client.probe_post(operation, payload)
        self._note(f"{suite}-response", operation, record)
        # Attach BEFORE any fallback fires: both attempts are legitimate
        # recorded evidence, and each manifest row must have a name-aligned
        # artifact holding ITS body (review finding).
        self._attach(f"{suite}-response.json", _artifact_content(record), log)
        if payload is None and record["status"] == 400:
            # Documented shape fallback for no-input RPCs, not a retry policy:
            # some releases insist on an explicit empty JSON object.
            self.logger.info("Bodiless POST returned 400 — retrying once with '{}'.", extra=log)
            record = client.probe_post(operation, {})
            self._note(f"{suite}-response-braces", operation, record)
            self._attach(f"{suite}-response-braces.json", _artifact_content(record), log)
            # The note reports the observed outcome only — this artifact must
            # never claim acceptance the job did not check (review finding).
            outcome = (
                f"HTTP {record['status']}"
                if record["status"] is not None
                else f"transport error: {record['error']}"
            )
            self._attach(
                f"{suite}-request-braces.json",
                json.dumps(
                    {
                        "operation": operation,
                        "body": {},
                        "note": f"retry after bodiless 400; {outcome}",
                    }
                ),
                log,
            )
        self.logger.info(_record_line(f"{suite} response", record), extra=log)

        time.sleep(POST_AFTER_DELAY_SECS)
        after = client.probe_get(WATCH_PROBES[0][1])
        self._note(f"{suite}-after", WATCH_PROBES[0][1], after)
        self._attach(f"{suite}-after.json", _artifact_content(after), log)
        self._log_pred_summary(0, after, log)
        self.logger.info(
            "POST suite complete: one RPC sent, before/response/after captured. This job "
            "does not retry, chain, or interpret — decide next steps from the artifacts.",
            extra=log,
        )

    # ------------------------------------------------------------- analyses --
    # Everything below LOGS observations for convenience; nothing gates.

    def _snapshot_analyses(self, parsed, texts, log):
        # Counts come from the UNTRUNCATED probe texts, never the attached
        # artifacts — a cut capture would make these silently-short totals.
        pred_entries = len(_WTP_MAC_RE.findall(texts.get("P3-predownload", "")))
        joined = len(_WTP_MAC_RE.findall(texts.get("P4-capwap", "")))
        self.logger.info(
            "predownload-data lists %s AP entr%s; capwap-data lists %s joined AP%s. "
            "(The 'three worlds' question: does the predownload list cover every "
            "joined AP before any predownload has run?)",
            pred_entries,
            "y" if pred_entries == 1 else "ies",
            joined,
            "" if joined == 1 else "s",
            extra=log,
        )

        modes = set(_BOOT_MODE_RE.findall(texts.get("P1-install-oper", "")))
        if modes:
            self.logger.info(
                "boot-mode value(s) published: %s", ", ".join(sorted(modes)), extra=log
            )

        prime = list(_iter_key(parsed.get("P5-ap-oper-full") or {}, "ap-prime-info"))
        if prime:
            with_secondary = sum(
                1 for p in prime if isinstance(p, dict) and p.get("secondary-controller-name")
            )
            self.logger.info(
                "ap-prime-info present for %s AP(s); %s have a secondary controller configured.",
                len(prime),
                with_secondary,
                extra=log,
            )
        else:
            self.logger.info("ap-prime-info not found in any captured AP data.", extra=log)

        for part in _iter_key(parsed.get("P7-q-filesystem") or {}, "partitions"):
            for entry in part if isinstance(part, list) else [part]:
                if isinstance(entry, dict) and "total-size" in entry:
                    try:
                        total = int(entry.get("total-size", 0))
                        used = int(entry.get("used-size", 0))
                    except (TypeError, ValueError):
                        continue
                    self.logger.info(
                        "partition %s: total %s KB, used %s KB, free %s KB",
                        entry.get("name", "?"),
                        total,
                        used,
                        total - used,
                        extra=log,
                    )

    def _log_pred_summary(self, seq, record, log):
        counts = {}
        for status in _PRED_STATUS_RE.findall(record["text"]):
            counts[status] = counts.get(status, 0) + 1
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no entries"
        self.logger.info(
            "poll %03d predownload: %s (%s)", seq, summary, _record_line("read", record), extra=log
        )

    # -------------------------------------------------------------- plumbing --

    def _probe_get(self, client, label, path, log):
        record = client.probe_get(path)
        self._note(label, path, record)
        self.logger.info(_record_line(label, record), extra=log)
        self._attach(f"{label}.json", _artifact_content(record), log)
        return record

    def _attach_manifest(self, test_suite, log):
        self._attach(
            "00-manifest.json",
            json.dumps({"suite": test_suite, "probes": self._records}, indent=1),
            log,
        )

    def _note(self, label, path, record):
        # Every row carries a UTC wall-clock: the manifest is the run's ledger,
        # and the bench's fire-time/done-time questions need times on EVERY
        # poll — including unchanged ones (review finding).
        self._records.append(
            {
                "label": label,
                "path": path,
                "status": record["status"],
                "elapsed_ms": record["elapsed_ms"],
                "bytes": record.get("content_bytes", len(record["text"])),
                "error": record["error"],
                "at": _utc_now(),
            }
        )
        if self._hunt:
            if record["text"]:
                self._hunt_searched += 1
                if self._hunt in record["text"].lower():
                    self._hunt_hits.append(label)
            else:
                self._hunt_empty += 1

    def _attach(self, filename, content, log):
        # Truncate on ENCODED bytes — the platform's JOB_CREATE_FILE_MAX_SIZE is
        # enforced on utf-8 bytes, not characters.
        encoded = len(content.encode("utf-8"))
        if encoded > MAX_ARTIFACT_BYTES:
            cut = content.encode("utf-8")[:MAX_ARTIFACT_BYTES].decode("utf-8", errors="ignore")
            content = cut + f"\n... [TRUNCATED by Dev Tester at {MAX_ARTIFACT_BYTES} bytes]"
            encoded = MAX_ARTIFACT_BYTES
            self.logger.warning(
                "Artifact %s truncated at %s bytes.", filename, MAX_ARTIFACT_BYTES, extra=log
            )
        # Aggregate budget: a long watch on a big roster must degrade to a
        # manifest-only ledger, not exhaust JobResult storage or the worker.
        if self._attached_bytes + encoded > TOTAL_ARTIFACT_BUDGET:
            self.logger.warning(
                "Artifact budget (%s bytes) exhausted — %s NOT attached; further probes "
                "are recorded in the manifest only.",
                TOTAL_ARTIFACT_BUDGET,
                filename,
                extra=log,
            )
            return
        self._attached_bytes += encoded
        if not hasattr(self, "create_file"):
            self.logger.warning(
                "Could not attach %s: this Nautobot lacks Job.create_file — the payload "
                "was NOT preserved (manifest row only).",
                filename,
                extra=log,
            )
            return
        try:
            self.create_file(filename, content)
        except Exception as exc:  # noqa: BLE001 - artifact failure may never kill the run
            # House pattern (_attach_health_artifact): create_file can raise on
            # size caps or storage errors; the recording must survive it.
            self.logger.warning("Could not attach %s: %s", filename, exc, extra=log)

    @staticmethod
    def _device_host(device):
        primary = device.primary_ip4 or device.primary_ip
        if not primary:
            raise DevTesterAbort("Device has no primary IP address.")
        return str(primary.host)

    def _credentials(self, device, override_group, log):
        # Same resolution order as the upgrade job (to be unified when the
        # shared install_engine module lands): override group, else the
        # device's own Secrets Group; RESTCONF/HTTP/Generic access types.
        group = override_group or device.secrets_group
        if not group:
            raise DevTesterAbort(
                "No Secrets Group assigned to the device and no override provided."
            )
        username = self._secret(group, device, SecretsGroupSecretTypeChoices.TYPE_USERNAME)
        password = self._secret(group, device, SecretsGroupSecretTypeChoices.TYPE_PASSWORD)
        return username, password

    @staticmethod
    def _secret(group, device, secret_type):
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
                continue
            except Exception as exc:  # noqa: BLE001 - real backend/decryption error
                raise DevTesterAbort(
                    f"Error retrieving the '{secret_type}' secret from Secrets Group "
                    f"'{group}' ({access_type}): {exc}"
                ) from exc
        raise DevTesterAbort(
            f"No '{secret_type}' secret defined in Secrets Group '{group}' for any of the "
            "RESTCONF/HTTP/Generic access types."
        )
