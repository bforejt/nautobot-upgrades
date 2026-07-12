# What the upgrade does (overview)

A plain, high-level summary of the **Cisco IOS-XE Upgrade (RESTCONF)** job — the
six phases and the three ways a device's run can end. For the full gate-by-gate
decision logic (every abort and warning), see
[upgrade-flow.md](upgrade-flow.md).

![IOS-XE upgrade — high-level overview](overview-flow.svg)

## How to read it

- **Blue** = start. **White boxes** = the phases. **Diamonds** = decisions.
- The **numbered key** to the left of each white box is its phase number in the
  README's "What it does" six-phase list (Install spans two boxes — both **4**;
  the commit box does both verify-commit and sync — **5·6**).
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
