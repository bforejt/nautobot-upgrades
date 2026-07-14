# What the upgrade does (overview)

A plain, high-level summary of the **Cisco IOS-XE Upgrade (RESTCONF)** job — the
seven core phases (plus the opt-in **8a/8b** health-check bracket) and how a
device's run can end (dry-run, staged, committed, or rolled back). For the full gate-by-gate decision logic (every abort and
warning), see [upgrade-flow.md](upgrade-flow.md).

![IOS-XE upgrade — high-level overview](overview-flow.svg)

## How to read it

- **Blue** = start. **White boxes** = the phases. **Diamonds** = decisions.
- The **numbered key** to the left of each white box is its phase number in the
  README's "What it does" list (one key per phase — `install add` and activate
  are distinct phases; commit and sync are distinct blocks). **8a/8b** are the
  opt-in pre/post health checks: 8a captures the baseline just before
  activation (a failed read aborts while aborting is still free), 8b compares
  against it after the sync, convergence-aware and report-only.
- **Green** = a successful end state. There are four, matching the **Run scope**
  input: a Dry-run report (no changes); a **Step 1** stop (image copied to flash,
  nothing else); a **Steps 1 & 2** stop (image also `install add`ed and marked
  for activation, but not reloaded); or a **Full** run all the way to a
  completed, committed upgrade.
- **Red** = this device stops here: it did not come back on the target version,
  so the job does **not** commit and the device auto-rolls-back to its previous
  image.
- The job runs this flow independently for each selected device; a device that
  stops does not stop the batch, but any device stopping marks the whole Job
  Result FAILED at the end.

Editable source: [overview-flow.drawio](overview-flow.drawio).
