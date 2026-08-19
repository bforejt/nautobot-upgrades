# Dev tooling: the RESTCONF Dev Tester

An **evidence recorder** for bench work and field diagnostics: point it at ONE
device and it captures raw RESTCONF responses, HTTP statuses, and read timings
as Job Result file artifacts, with a UTC-stamped manifest ledger of every
request. It records — it never decides, retries, chains, or interprets. It was
built for the Catalyst 9800 AP-predownload bench (2026-08), where it replaced
a seventeen-probe manual Postman sheet, and it stays general: future platform
bring-up and "run the Dev Tester, send me the Job Result" field diagnostics.

## Not part of the upgrader

The Dev Tester is **opt-in per environment** and does not appear in the jobs
list at all unless the environment declares itself a development one. On the
**Nautobot worker**, set:

```
NAUTOBOT_UPGRADES_DEVTOOLS=1
```

(accepted values: `1`, `true`, `yes`), then re-sync the Git repository. Unset
the variable and re-sync to remove it again — production instances that never
set the flag never even import the module.

When the flag is removed after use, Nautobot keeps the job's database record
and marks it "not installed"; that record is harmless and disappears if
deleted from the Jobs admin.

## What it offers (summary)

- **GET suites** (strictly read-only): a snapshot pack (install/stack/AP
  oper reads with log-only convenience analyses, plus custom `data/...`
  paths, positively validated) and a watch recorder (save-on-change polling
  with steady-state early stop).
- **POST suites** (each sends exactly ONE RPC, behind an explicit
  acknowledgement checkbox): the AP-predownload fire/abort/clear and the
  upgrade dry-run RPC.
- Every capture is attached to the Job Result; `00-manifest.json` records
  each request's status, elapsed time, byte count, and UTC timestamp. An
  optional `hunt_string` searches every capture (e.g. for a fired uuid).

The design contract and its review history live in the module docstring of
[`jobs/restconf_dev_tester.py`](../jobs/restconf_dev_tester.py).

## Care and feeding

Captures can carry internal hostnames, IPs, and serials — they belong in Job
Results and private storage, never in this public repository (the repo's
`.gitignore` refuses the common export shapes as a backstop).
