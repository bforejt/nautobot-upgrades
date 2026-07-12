"""Cancel a running IOS-XE Upgrade run — gracefully.

Native job cancellation is landing in **Nautobot core 3.2** (a "Stop Job
execution" control — nautobot/nautobot#2088, closed COMPLETED for the v3.2
milestone). It is NOT yet on every supported train, though: the 2.4 LTM line
and 3.1 both predate 3.2 and have no native control, and until now core had
none at all. This companion Job stays the git-deliverable way to cancel a run
UNTIL every supported Nautobot train can do it natively — revisit removing it
once the supported floor is 3.2+. (A UI button on older trains would otherwise
require a full Nautobot App.)

Pick the running Job Result and run — it signals the upgrade run with the same
mechanism as Celery's soft time limit, which the upgrade job already handles
COOPERATIVELY: every in-flight device stops at its next safe step boundary
(never mid-decision), queued devices are cancelled, every device's outcome is
drained into the log, and the run fails honestly with a
completed/stopped/never-started post-mortem. Devices are left in states the
idempotent gates recover on re-run.
"""

from __future__ import annotations

from nautobot.apps.jobs import Job, ObjectVar
from nautobot.extras.choices import JobResultStatusChoices
from nautobot.extras.models import JobResult

name = "IOS-XE Upgrades"


class CancelUpgradeRun(Job):
    """Gracefully stop a running or queued Cisco IOS-XE Upgrade job run."""

    target_result = ObjectVar(
        model=JobResult,
        label="Running job result",
        description=(
            "The Job Result to cancel. In-flight device upgrades stop at their "
            "next SAFE step boundary (within about one poll interval); queued "
            "devices never start; the cancelled run logs a full post-mortem. "
            "Devices already mid-upgrade are recovered by simply re-running "
            "the upgrade job later (idempotent gates + commit-to-be-safe)."
        ),
        query_params={
            "status": [
                JobResultStatusChoices.STATUS_STARTED,
                JobResultStatusChoices.STATUS_PENDING,
            ]
        },
    )

    class Meta:
        name = "Cancel IOS-XE Upgrade Run"
        description = (
            "Gracefully cancel a running 'Cisco IOS-XE Upgrade (RESTCONF)' run: "
            "devices stop at safe boundaries and the run reports what completed, "
            "what stopped, and what never started."
        )
        has_sensitive_variables = False

    def run(self, *, target_result):
        status = target_result.status
        if status not in (
            JobResultStatusChoices.STATUS_STARTED,
            JobResultStatusChoices.STATUS_PENDING,
        ):
            raise RuntimeError(
                f"Job Result {target_result.id} is not cancellable — its status "
                f"is '{status}' (only running or queued runs can be cancelled)."
            )
        job_name = str(getattr(target_result, "name", "") or "")
        if getattr(self, "job_result", None) is not None and str(target_result.id) == str(
            self.job_result.id
        ):
            raise RuntimeError("Refusing to cancel this Cancel job's own run.")
        # Match the UPGRADE job specifically: the old 'IOS-XE' substring check
        # whitelisted every sibling job in this repo, including this Cancel
        # job itself, none of which implement the cooperative stop (review
        # finding).
        if "Cisco IOS-XE Upgrade" not in job_name:
            # Only warn: the graceful signal is only KNOWN-safe for our upgrade
            # job (whose loops implement the cooperative stop); other jobs get
            # celery's default soft-limit behavior, which may be less graceful.
            self.logger.warning(
                "Target '%s' does not look like an IOS-XE Upgrade run. Sending "
                "the stop signal anyway — but only the upgrade job guarantees "
                "safe-boundary shutdown semantics.",
                job_name or target_result.id,
            )

        from nautobot.core.celery import app as celery_app

        if status == JobResultStatusChoices.STATUS_PENDING:
            # Never started: a plain revoke prevents it from ever running.
            celery_app.control.revoke(str(target_result.id))
            self.logger.info("Queued run %s revoked — it will not start.", target_result.id)
            return f"Revoked queued run {target_result.id} (never started)."

        # Running: SIGUSR1 raises SoftTimeLimitExceeded in the task's main
        # thread — the exact signal the upgrade job's cooperative-stop path is
        # built for (safe boundaries, drained results, post-mortem).
        celery_app.control.revoke(str(target_result.id), terminate=True, signal="SIGUSR1")
        self.logger.info(
            "Stop signal sent to run %s. In-flight devices stop at their next "
            "safe checkpoint (roughly one poll interval, ~30s; up to ~2 minutes "
            "if an RPC is mid-flight). Watch that Job Result for the "
            "completed / stopped / never-started post-mortem, then re-run the "
            "upgrade job later for the stopped devices — the gates make "
            "re-runs safe.",
            target_result.id,
        )
        return (
            f"Graceful stop signalled to run {target_result.id} — see its "
            "post-mortem for per-device outcomes."
        )
