# Engineering internals

The deep forensic material behind the README's summaries: why the code reads
the device the way it does, what was measured on real hardware, and which
dead ends are closed on purpose. Nothing here is needed to *operate* the
jobs — it exists so future changes are made with the evidence in view instead
of re-learning it. Constants carry their own provenance notes in
[`jobs/constants.py`](../jobs/constants.py); the shared machinery both jobs
inherit lives in [`jobs/install_engine.py`](../jobs/install_engine.py).

## The SELinux AVC story

**The symptom.** On affected platforms/releases — observed so far only on
Catalyst 9300 switches (17.15.x, 17.18.3); a full C8000V upgrade showed none,
as did the 9800-CL bench — the platform's SELinux policy denies `smand` (the
shell/storage manager) read access to a handful of on-flash paths
(`biosupgrade`, `yang-infra`, and similar) that it touches whenever it builds
a **filesystem listing**. Each listing sprays a burst of
`%SELINUX-1-VIOLATION` AVC-denial lines (~100 observed per listing on a real
9300). Anything that walks the filesystem trips it — including an operator's
`dir`.

**What the correlations showed.** Lining the job's log against the device
console: in one capture ~318 of 319 denials came from the job's own
q-filesystem reads (one from an `install remove` itself); a later complete
run (copy → add → activate → commit) settled it — **1,618 denials, every one
during the job's read phases; `install add`, `activate`, and `commit`
contributed zero**. The copy watcher's per-poll full listings alone were ~91%
of the noise.

**What Cisco documents — honestly.** `%SELINUX-1-VIOLATION` is a documented
IOS-XE message (see the *Support for Security-Enhanced Linux* chapter in the
platform config guides). Cisco lists it as an **alert-level** event whose
recommended action is to contact TAC — *not* "ignore it" — and the Bug Search
Tool has both genuinely harmful instances (process crashes, install failures:
CSCwk19620, CSCwt91818) and case-specific benign ones (CSCwr09316, "can
safely be ignored"). A direct BST hunt for **our exact denial** (`smand`,
`biosupgrade`/`yang-infra`, filesystem listing, 9300) returned **no matching
defect**. So the honest posture: across every lab upgrade the burst appeared
and **no operation ever failed** — *benign in our testing, not a
Cisco-confirmed cosmetic defect*. If a burst ever coincides with a real
failure, open a TAC case.

**How the job minimizes them (measured).** The design is *walk-free on the
happy path*:

- The copy pre-check decides **both skip and absence** without a walk: the
  engine's package inventory names the file, keyed single-entry reads
  corroborate the byte count, and absence is decided by keyed probes of up
  to three ranked device-published candidate directories — bench-proven
  AVC-silent for hits **and** misses (2026-07-30). A clean miss is accepted
  as absence because its worst case is one harmless overwriting re-transfer;
  the mid-transfer watch never gets that license (a keyed miss there proves
  nothing).
- The classic-copy watcher **learns, then goes quiet**: it full-reads only
  until it sights the growing file, then polls that entry's own published
  address — with a loud full-listing fallback latched after a rejected URL
  form or two consecutive misses. The address is pure observation; the
  guessed-mount-root tiers were deleted (2026-07-10) and stay deleted.
- The xcopy watch rides the install-oper ledger plus a keyed address
  **constructed from device-published state** (the download descriptor's
  dest-dir/dest-filename + the partition-stats keys).
- The one remaining read with any AVC cost is the shared partition-stats
  read (discovery + free-space gate): bench-measured 2026-07-30 at **~2
  mount-level statfs denials** (`"/"`, `mnt_t`) — *not* a file-enumeration
  walk. Measured on a single lab 9300; per-member counts on a stack/SVL are
  unmeasured (possibly a few more lines, still never a walk).

Expected profile with the default transfer: **~2 AVC lines per run**. On the
classic-copy fallback tier a fresh copy adds a handful of real listings (the
pre-check, the watcher's reads until first sighting, the final verify) —
versus the pre-rework behavior of one full listing every 30 seconds for the
whole transfer.

**Two device-tested dead ends, closed on purpose:** RFC 8040 `depth` is a
**post-filter** on this backend (returns pruned output, still collects and
denies server-side — not an optimization), and trimming the partition-stats
projection to names-only returned denials AND no response. Do not "optimize"
either read.

**The quieting workaround.** The opt-in job input inserts an `NBAVC` logging
discriminator into running-config as early as possible and attaches it to
the **physical console and terminal-monitor sessions only** — `show logging`
and syslog hosts deliberately stay complete (they are the record; genuine
SELinux events share the facility). It never replaces an operator's
discriminator or flips logging modes, warns instead of failing on refusal,
and is erased by the activation reload unless combined with the pre-reload
save. Manual equivalent:

```
logging discriminator NOSEL msg-body drops SELINUX
logging console discriminator NOSEL
logging buffered discriminator NOSEL
```

## The xcopy transfer watch

Why it exists: the classic `copy` RPC is blocking, and the device's
management plane (DMI/ConfD) kills any blocking RPC at roughly **600
seconds** — an internal, non-configurable ceiling (not
`ip http timeout-policy`). Slow-WAN Step 1s deterministically died at
`HTTP 400 "application timeout"`. The async fire returns immediately, so the
ceiling never applies.

**The fire.** `Cisco-IOS-XE-xcopy-rpc:xcopy` with the device-side `timeout`
leaf **always set** (omitting it lands 0 in the download descriptor — an
instantly-expired window, bench-proven), a **bare filename** destination
(prefixes poison the descriptor), and a **port-less** source URL (an explicit
`:port` fails inside the device's express-copy parser before a single packet
is sent — wire-proven; it is a pre-fire fallback guard, not a runtime
surprise).

**The watch.** Success is **the engine's published verdict, confirmed
byte-exact**: the uuid's ledger record (in-flight under `install-oper`,
terminal in `install-oper-hist` with `install-op-succ`/`-fail`), normally
confirmed from the engine's own **package inventory** (exact byte size,
`verify-ok`, an operation-gated timestamp; entries vanish when files are
deleted). Some trains verify lazily (`install-package-verify-deferred`,
field-observed on a 9500 SVL) — the confirm waits a bounded beat of zero-AVC
ledger re-polls, then tries a keyed byte-exact read of the destination
(positive-accept only), with the authoritative listing as the floor. Failure
is **the engine's published failing transaction** (e.g.
`install-txn-download → fail`) — a device reason, never an inference.

**Job-side declarations are fallback tiers only**: the fire-lost bound
(readable ledger polls — counted, never wall time — that never show the
uuid, confirmed by a fresh authoritative listing that positively lacks the
destination file) and the transfer-window deadline (a backstop past the
RPC's own on-device timeout). **Ambiguous ends never fall back to classic
copy** — deadline, stop/cancel, unreadable-poll streaks, a vanished ledger
record, any post-fire change to the destination file, an unreadable
declaration listing, or a same-named wrong-size file: the engine may still
be writing, and a fallback would put two writers on one file. A job stop
cannot stop the device-side transfer; the engine-idle gate makes the next
run wait it out safely.

**The removed engine-download experiment (2026-07).** A third method —
handing the URL to `install add` itself — was bench-validated end-to-end and
then removed: with xcopy default and classic copy covering ported-URL
servers, it had no niche worth a third per-train bench matrix. Findings
retained: the install model's `download-timeout` leaf is interpreted roughly
as *seconds* despite the modeled minutes (sending 10 strangled a healthy
transfer; the device default ~2000 applies when omitted) — the same
seconds-vs-minutes confusion shapes how the xcopy timeout leaf is sent.

## The 9800 predownload design, from the bench

The wireless job's gates were built from a recorded bench arc (2026-08-03/04,
9800-CL 17.15.5 → 17.18.3, live APs, an interrupted-AP negative case) rather
than from the YANG or the docs — both of which the bench contradicted in
places. A second arc (2026-08-05, same bench, target **17.15.4d**) then ran
the finished job end-to-end against a rebuild-letter target — the first live
exercise of the learned-quad gate, reported successful by the field tester.
The load-bearing findings:

- **`predownload-data` is an activity list, not a roster mirror** (empty —
  HTTP 204 — until a predownload engages), and an AP whose CAPWAP session
  dies mid-download **vanishes from it** rather than flipping to failed (the
  CLI aggregate counted "Failed: 1" while the RESTCONF entry disappeared).
  Hence the **R0 roster contract**: the joined-AP snapshot at fire time is
  what the gate judges — a gate over "entries currently present" would have
  passed wrongly the moment the AP vanished.
- **The AP-side target identity is learned from the device, never derived
  from the version string.** After `install add`, `ap-image-prepare-location`
  publishes the staged bundle's exact AP image quad; the staged quad is the
  set difference against `ap-image-active-location` (exactly one candidate,
  or refuse). This is what makes rebuild-letter targets work (a letter has
  no numeric form) and makes the comparison full 4-field identity — a base
  release can never satisfy a rebuild's gate.
- **Corroborators**: each AP publishes its backup-partition version
  (flipping to the target on completion — also the already-held skip tier
  for idempotent re-runs) and per-partition health. The fired uuid is
  echoed **nowhere** in oper data (hunted; the only echo lives in an
  untested events stream), so tracking is the state join, not a ledger.
- **Aggregate counters are corroboration only, never a gate** — the summary
  container exists (even where the YANG note claims EWC-only) and reads
  `num-complete == num-total == 0` on an idle controller: the `0 == 0` trap
  is live on real hardware.
- **A standalone 9800-CL publishes `stack-mode: mode-active-standby`,
  `topology: one-plus-one`, and `sso-ready-flag: false` as constants** —
  none indicate a pair. The topology gate identifies standalone positively:
  exactly one chassis entry, role-active, state-ready. Gating on the mode
  strings would have refused every healthy standalone.
- **Fire semantics**: accept is 204-empty (the RPC has no output — RFC 8040
  mandates it; bench-confirmed); refusal is an HTTP 4xx whose *status class*
  is the signal — the error text is unusable (a semantic refusal arrived as
  `malformed-message` + an EAGAIN string). Re-fire after completion is an
  accepted no-op. `clear-ap-predownload-statistics` is **not implemented
  over RESTCONF** on 17.15.5 ("Not supported by application 'wireless'") —
  the schema exists; the app refuses. Post-activate, APs **auto-swap** to
  the predownloaded partition (confirmed: the AP returned already running
  the target at the first post-reload poll), the reload wipes
  `predownload-data`, and a straggler doing the slow path shows
  `ap-operation-state: downloading` with **empty version fields**.
- **Failed can mean retrying**: the device's per-AP retry machinery is real
  (schema retry fields; field transcripts show Failed with a live retry
  timer) — so `failed` mid-window keeps watching, and only the deadline
  condemns. Engagement is WNCD-batched on real fleets (per-WNCD dispatch
  sets; a deferred AP logs "back off and retry"), so not-engaged-early is
  normal and the engagement check measures *change* from the pre-fire
  snapshot.

The bench instrument that recorded all of this — an evidence-recorder job —
is retired and archived with its design contract and a reconstruction guide:
[docs/archive/restconf-dev-tester/](archive/restconf-dev-tester/). The raw
captures live on as Job Result artifacts in the test environment and as the
scenario battery's fixtures.

## Version identity discipline

Rebuild letters are load-bearing everywhere: `17.15.4d` is **not**
`17.15.4`. The switch job's idempotency and staged-version comparisons key on
a parser that keeps the letter; the activate uses the device's **full
internal version string** (activating by the short marketing form hangs the
RPC on rebuild releases — bench-proven); and the 9800 job compares learned
integer quads for the same reason. Any future comparison that drops the
letter or truncates the quad is a regression against a real, observed
failure class.
