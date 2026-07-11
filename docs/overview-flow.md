# What the upgrade does (overview)

A plain, high-level summary of the **Cisco IOS-XE Upgrade (RESTCONF)** job — the
six phases and the three ways a device's run can end. For the full gate-by-gate
decision logic (every abort and warning), see
[upgrade-flow.md](upgrade-flow.md).

![IOS-XE upgrade — high-level overview](overview-flow.svg)

## How to read it

- **Blue** = start. **White boxes** = the phases. **Diamonds** = decisions.
- **Green** = a successful end state: a Dry-run report (no changes), a staged
  image (nothing reloaded), or a completed, committed upgrade.
- **Red** = this device stops here: it did not come back on the target version,
  so the job does **not** commit and the device auto-rolls-back to its previous
  image.
- The job runs this flow independently for each selected device; a device that
  stops does not stop the batch, but any device stopping marks the whole Job
  Result FAILED at the end.

Editable source: [overview-flow.drawio](overview-flow.drawio).
