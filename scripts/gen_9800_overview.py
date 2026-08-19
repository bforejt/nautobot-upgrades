#!/usr/bin/env python3
"""Generate docs/9800-overview-flow.drawio (editable) and .svg (image):
a HIGH-LEVEL 'what it does' overview of the Cisco 9800 WLC Upgrade job.

Companion to gen_overview.py (the switch job's overview, whose rendering
machinery this reuses verbatim) — same visual language, wireless spine:
the topology gate, the blast-radius echo, and the AP-predownload phase with
its own zero-impact stop are what differ from the switch flow. Mirror of
jobs/c9800_upgrade.py's _upgrade_controller() at overview altitude; keep
them in sync in the same change.
"""

import html
import os

CX = 340  # spine center x
PITCH = 108  # row center-to-center
TOP = 78  # first row center y

GEOM = {
    "start": (360, 56),
    "proc": (380, 66),
    "runopt": (400, 72),
    "dec": (260, 88),
    "end": (330, 56),
}
RIGHT_X = 590  # left edge of the right-hand terminal boxes
TERM_W, TERM_H = 330, 72

# spine, top to bottom. Each: id, type, text, branch-or-None.
#   branch = {"cond": <right-edge label>, "pass": <down-edge label>,
#             "kind": "okr"|"abort"|"opt", "text": <terminal box text>}
SPINE = [
    (
        "start",
        "start",
        "Select controller(s) + target version\n(Full: ONE controller, picked explicitly)",
        None,
    ),
    (
        "gcbefore",
        "runopt",
        "Opt-in, once per RUN: Golden Config backup of\nevery controller BEFORE any upgrade starts\n(fail-closed: can't back up → nothing is touched)",
        None,
    ),
    (
        "connect",
        "proc",
        "Connect & authenticate\n(RESTCONF over HTTPS, creds from Nautobot Secrets)",
        None,
    ),
    ("gates", "proc", "Pre-flight gates\n≥ 17.9.1 · install mode", None),
    (
        "d_topo",
        "dec",
        "Positively\nSTANDALONE?\n(one chassis,\nactive, ready)",
        {
            "cond": "No",
            "pass": "Yes",
            "kind": "abort",
            "text": "Predownload/Full REFUSED, naming what was\nread (HA SSO orchestration is unvalidated;\nsilence is never standalone) — staging\nscopes proceed with a warning",
        },
    ),
    (
        "echo",
        "proc",
        "Blast-radius echo (also in dry-run):\njoined APs + models + how many have a\nbackup controller (device-published prime info)",
        None,
    ),
    (
        "d_clean",
        "dec",
        "Clean device first\nopted in?",
        {
            "cond": "Yes",
            "pass": "No",
            "kind": "opt",
            "text": "Reclaim flash up front (install remove\ninactive; never in a dry-run) before the\nfree-space gate evaluates what's left",
        },
    ),
    ("freespace", "proc", "Free-space gate\n(on the cleaned flash when cleaning ran)", None),
    (
        "d_dry",
        "dec",
        "Dry-run?",
        {
            "cond": "Yes",
            "pass": "No",
            "kind": "okr",
            "text": "DONE: Dry-run — every pre-flight gate\nevaluated, roster + echo previewed;\nno changes made",
        },
    ),
    (
        "copy",
        "proc",
        "Transfer image + verify exact size\n(async xcopy by default, ledger-tracked;\nclassic copy = fallback tier; skipped if already on flash)",
        None,
    ),
    (
        "d_stagecopy",
        "dec",
        "Run scope =\nStep 1 (copy) only?",
        {
            "cond": "Yes",
            "pass": "No",
            "kind": "okr",
            "text": "DONE: Staged (Step 1) — image\ncopied to bootflash; nothing else",
        },
    ),
    (
        "add",
        "proc",
        "install add — extract & stage the bundle\n(gate → track via the device's ledger)",
        None,
    ),
    (
        "learn",
        "proc",
        "LEARN the target's AP image version from\nthe device (the staged bundle publishes it —\nrebuild letters OK) + model-coverage advisory",
        None,
    ),
    (
        "d_stageadd",
        "dec",
        "Run scope =\nSteps 1 & 2\n(copy + prep)?",
        {
            "cond": "Yes",
            "pass": "No",
            "kind": "okr",
            "text": "DONE: Staged (Steps 1 & 2) — added\n& marked for activation; not reloaded",
        },
    ),
    (
        "predl",
        "proc",
        "AP PREDOWNLOAD: snapshot the joined-AP\nroster (the contract) → fire → watch the\ncontroller's per-AP status to completion",
        None,
    ),
    (
        "d_complete",
        "dec",
        "EVERY roster AP\nholds the target?\n(per-AP complete, or\nalready in backup\npartition)",
        {
            "cond": "No",
            "pass": "Yes",
            "kind": "abort",
            "text": "The deadline DECLARES FAILURE, naming\neach incomplete/vanished AP — nothing\nactivates (the named-exception checkboxes\nare the deliberate overrides — tolerate\napplies to Full runs; stragglers take the\nslow post-reload path)",
        },
    ),
    (
        "d_stagepre",
        "dec",
        "Run scope =\nSteps 1-3\n(predownload)?",
        {
            "cond": "Yes",
            "pass": "No",
            "kind": "okr",
            "text": "DONE: PREDOWNLOADED — every joined AP\nholds the image; the window run needs\nonly Full (activate → reload → commit)",
        },
    ),
    (
        "d_healthpre",
        "dec",
        "Health checks\nopted in?",
        {
            "cond": "Yes",
            "pass": "No",
            "kind": "opt",
            "text": "Pre-test: baseline the AP roster, per-AP\nversions, radio states, reload reason\n(a failed read aborts BEFORE activate)",
        },
    ),
    (
        "d_savepre",
        "dec",
        "Save config before\nreload opted in?",
        {
            "cond": "Yes",
            "pass": "No",
            "kind": "opt",
            "text": "Save the running config (the activation\nreload never prompts — unsaved changes\nwould be lost); the device's RPC result verifies",
        },
    ),
    (
        "activate",
        "proc",
        "Activate (non-ISSU) → controller reloads;\nEVERY joined AP reboots to swap to its\npredownloaded partition",
        None,
    ),
    (
        "d_boot",
        "dec",
        "Controller booted\nthe target &\ncame back?",
        {
            "cond": "No",
            "pass": "Yes",
            "kind": "abort",
            "text": "Auto-rollback to the previous image —\nNOT committed (a rollback would reboot\nthe AP fleet a second time)",
        },
    ),
    (
        "commit",
        "proc",
        "install commit — CONTROLLER-side facts only\n(AP rejoin NEVER gates the commit:\nrefusing would guarantee a second fleet outage)",
        None,
    ),
    (
        "sync",
        "proc",
        "Sync Nautobot software version\n(the controller record now shows the new OS)",
        None,
    ),
    (
        "rejoin",
        "proc",
        "AP rejoin report (REPORT-ONLY): who is back\non the target, who is downloading (slow path),\nwho is missing (maybe on its backup controller)",
        None,
    ),
    (
        "d_savepost",
        "dec",
        "Save config after\ncommit opted in?",
        {
            "cond": "Yes",
            "pass": "No",
            "kind": "opt",
            "text": "Save again on the NEW OS — normalizes\nstartup to the new version's rendering\n(soak trade-off in the README; default off)",
        },
    ),
    (
        "d_cleanup",
        "dec",
        "Remove inactive\nopted in?",
        {
            "cond": "Yes",
            "pass": "No",
            "kind": "opt",
            "text": "install remove inactive — reclaim flash\nspace (old packages & prior images);\ngated & tracked like every engine write",
        },
    ),
    (
        "d_healthpost",
        "dec",
        "Health checks\nopted in?",
        {
            "cond": "Yes",
            "pass": "No",
            "kind": "opt",
            "text": "Post-test: compare vs the baseline (AP roster\nas a NAMED set difference · per-AP versions ·\nradios up-before · reload reason; report-only)",
        },
    ),
    ("done", "end", "DONE: Upgraded & committed ✓\n(the fleet rebooted once, image pre-staged)", None),
    (
        "gcafter",
        "runopt",
        "Opt-in, once per RUN: Golden Config backup\nagain AFTER all controllers finish (runs even\nif some failed; warn-only — never un-succeeds)",
        None,
    ),
]

# No numbered phase rail here: the README's 9800 section describes scopes, not
# numbered phases — badges would key to nothing.
PHASE_TAGS = {}

LEGEND = (
    "Legend\n"
    "diamonds = decisions; white rounded boxes to\n"
    "the right = opt-in steps — the flow continues\n"
    "down and rejoins the spine either way\n"
    "dashed = opt-in and once per RUN, not per device\n"
    "green = successful end state (one per Run scope)\n"
    "red = this controller stops here\n"
    "The switch job's overview: docs/overview-flow.svg\n"
    "Its detailed gate logic: docs/upgrade-flow.svg\n"
    "(shared engine — this diagram shows what differs)"
)

FILLS = {
    "start": ("#DAE8FC", "#6C8EBF"),
    "proc": ("#FFFFFF", "#5B6B7B"),
    "dec": ("#E8EEF6", "#3C6CA8"),
    "end": ("#D5E8D4", "#2E7D32"),
    "okr": ("#D5E8D4", "#2E7D32"),
    "abort": ("#F8CECC", "#B85450"),
    "opt": ("#FFFFFF", "#5B6B7B"),
    "runopt": ("#FFFFFF", "#5B6B7B"),
}


def esc(s):
    return html.escape(s, quote=True)


# Rows that FOLLOW an opt-in branch get extra pitch: the branch box's rejoin
# elbow needs air between the "No" label and the next node.
EXTRA_AFTER_OPT = 28
CY = {}
_y = TOP
_prev_opt = False
for _nid, _typ, _txt, _br in SPINE:
    if _prev_opt:
        _y += EXTRA_AFTER_OPT
    CY[_nid] = _y
    _y += PITCH
    _prev_opt = bool(_br) and _br.get("kind") == "opt"
NODES = {nid: (typ, text, branch) for nid, typ, text, branch in SPINE}
ORDER = [nid for nid, *_ in SPINE]
NEXT = {a: b for a, b in zip(ORDER, ORDER[1:])}

WIDTH = RIGHT_X + TERM_W + 40
HEIGHT = CY[ORDER[-1]] + 70

def svg_text(cx, cy, text, size=12, bold=False, color="#1a1a1a", anchor="middle"):
    ls = text.split("\n")
    lh = size + 3
    start = cy - (len(ls) - 1) * lh / 2
    weight = ' font-weight="bold"' if bold else ""
    out = [
        f'<text x="{cx}" y="{start:.0f}" text-anchor="{anchor}" '
        f'font-family="Helvetica,Arial,sans-serif" font-size="{size}"{weight} '
        f'fill="{color}">'
    ]
    for k, ln in enumerate(ls):
        dy = 0 if k == 0 else lh
        out.append(f'<tspan x="{cx}" dy="{dy:.0f}">{esc(ln)}</tspan>')
    out.append("</text>")
    return "".join(out)


def rect(cx, cy, w, h, fill, stroke, rx=6, dash=False):
    extra = ' stroke-dasharray="7 5"' if dash else ""
    return (
        f'<rect x="{cx - w / 2:.0f}" y="{cy - h / 2:.0f}" width="{w}" height="{h}" '
        f'rx="{rx}" ry="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{extra}/>'
    )


def diamond(cx, cy, w, h, fill, stroke):
    pts = f"{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}"
    return f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'


def arrow(x1, y1, x2, y2, color="#555", dash=False):
    extra = ' stroke-dasharray="7 5"' if dash else ""
    return (
        f'<path d="M {x1:.0f} {y1:.0f} L {x2:.0f} {y2:.0f}" fill="none" '
        f'stroke="{color}" stroke-width="1.5"{extra} marker-end="url(#arrow)"/>'
    )


def elbow(points, color="#555"):
    d = "M " + " L ".join(f"{x:.0f} {y:.0f}" for x, y in points)
    return (
        f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.5" marker-end="url(#arrow)"/>'
    )


def edge_label(x, y, text, color="#333"):
    w = max(len(text) * 6.5 + 8, 18)
    return (
        f'<rect x="{x - w / 2:.0f}" y="{y - 9:.0f}" width="{w:.0f}" height="18" rx="3" '
        f'fill="#ffffff" fill-opacity="0.85" stroke="none"/>'
        + svg_text(x, y + 4, text, size=11, color=color)
    )


TAG_H = 32
TAG_GAP = 16  # gap between the tag's right edge and the block's left edge
# Purple, deliberately NOT the blue used by start/decision nodes — the badge
# reads as an index key, not part of the flow.
BADGE_FILL, BADGE_STROKE, BADGE_TEXT = "#EDE3F6", "#8E63B5", "#4A2A7A"


def tag_geom(label, box_left, cy):
    """(center-x, center-y, width) for a phase badge right-aligned left of a block."""
    w = max(TAG_H, len(label) * 12 + 14)
    cx = (box_left - TAG_GAP) - w / 2
    return cx, cy, w


def phase_badge(label, box_left, cy):
    cx, cy, w = tag_geom(label, box_left, cy)
    # An svg <text> anchors on the baseline, so a single glyph centered on cy
    # sits slightly high in the circle; nudge it down to optically center it.
    return rect(cx, cy, w, TAG_H, BADGE_FILL, BADGE_STROKE, rx=TAG_H / 2) + svg_text(
        cx, cy + 6, label, size=17, bold=True, color=BADGE_TEXT
    )


def build_svg():
    s = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'width="{WIDTH}" height="{HEIGHT}" font-family="Helvetica,Arial,sans-serif">'
    ]
    s.append(
        '<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" '
        'refY="3" orient="auto" markerUnits="strokeWidth">'
        '<path d="M0,0 L8,3 L0,6 z" fill="#555"/></marker></defs>'
    )
    s.append(f'<rect x="0" y="0" width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>')
    s.append(
        '<text x="20" y="30" font-size="18" font-weight="bold" fill="#111">'
        "Cisco 9800 WLC Upgrade (IOS-XE) — what it does</text>"
    )
    s.append(rect(WIDTH - 195, 118, 380, 158, "#fbfbfb", "#bbb", rx=6))
    s.append(svg_text(WIDTH - 195, 118, LEGEND, size=10, color="#333"))

    # down edges between consecutive spine nodes
    for a, b in zip(ORDER, ORDER[1:]):
        ta = NODES[a][0]
        tb = NODES[b][0]
        y1 = CY[a] + GEOM[ta][1] / 2
        y2 = CY[b] - GEOM[tb][1] / 2
        s.append(arrow(CX, y1, CX, y2, dash="runopt" in (ta, tb)))
        branch = NODES[a][2]
        if branch:
            s.append(edge_label(CX + 16, (y1 + y2) / 2, branch["pass"]))

    # right-hand terminal branches off decisions
    for nid in ORDER:
        typ, _, branch = NODES[nid]
        if not branch:
            continue
        cy = CY[nid]
        dw = GEOM["dec"][0]
        kind = branch["kind"]
        color = {"okr": "#2E7D32", "abort": "#B85450", "opt": "#555"}[kind]
        s.append(arrow(CX + dw / 2, cy, RIGHT_X, cy, color=color))
        s.append(
            edge_label(
                (CX + dw / 2 + RIGHT_X) / 2,
                cy - 10,
                branch["cond"],
                color="#333" if kind == "opt" else color,
            )
        )
        bx = RIGHT_X + TERM_W / 2
        s.append(rect(bx, cy, TERM_W, TERM_H, *FILLS[kind], rx=22))
        if kind == "opt":
            s.append(svg_text(bx, cy, branch["text"], size=11, color="#1a1a1a"))
            # opt boxes are side-steps, not end states: rejoin the spine just
            # above the next node, below the "No" label
            nxt = NEXT[nid]
            next_top = CY[nxt] - GEOM[NODES[nxt][0]][1] / 2
            y_r = next_top - 9
            s.append(elbow([(bx, cy + TERM_H / 2), (bx, y_r), (CX, y_r)]))
        else:
            s.append(
                svg_text(
                    bx,
                    cy,
                    branch["text"],
                    size=11,
                    bold=True,
                    color="#1b5e20" if kind == "okr" else "#8a1f1f",
                )
            )

    # spine nodes on top
    for nid in ORDER:
        typ, text, _ = NODES[nid]
        cy = CY[nid]
        w, h = GEOM[typ]
        if typ == "dec":
            s.append(diamond(CX, cy, w, h, *FILLS["dec"]))
            s.append(svg_text(CX, cy, text, size=11, bold=True, color="#13335c"))
        elif typ in ("start", "end"):
            s.append(rect(CX, cy, w, h, *FILLS[typ], rx=24))
            s.append(
                svg_text(
                    CX,
                    cy,
                    text,
                    size=12,
                    bold=True,
                    color="#1b3a6b" if typ == "start" else "#14532d",
                )
            )
        elif typ == "runopt":
            s.append(rect(CX, cy, w, h, *FILLS["runopt"], rx=10, dash=True))
            s.append(svg_text(CX, cy, text, size=11, color="#1a1a1a"))
        else:
            s.append(rect(CX, cy, w, h, *FILLS["proc"], rx=6))
            s.append(svg_text(CX, cy, text, size=11, color="#1a1a1a"))

    # phase-number badges: one aligned rail keyed to the proc-block left edge
    rail_left = CX - GEOM["proc"][0] / 2
    for nid, label in PHASE_TAGS.items():
        s.append(phase_badge(label, rail_left, CY[nid]))

    s.append("</svg>")
    return "\n".join(s)


# ------------------------------------------------------------- drawio output --

DRAWIO_STYLE = {
    "start": "rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#DAE8FC;strokeColor=#6C8EBF;",
    "end": "rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#D5E8D4;strokeColor=#2E7D32;fontStyle=1;",
    "proc": "rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#5B6B7B;",
    "dec": "rhombus;whiteSpace=wrap;html=1;fillColor=#E8EEF6;strokeColor=#3C6CA8;fontStyle=1;",
    "okr": "rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#D5E8D4;strokeColor=#2E7D32;fontStyle=1;",
    "abort": "rounded=1;whiteSpace=wrap;html=1;fillColor=#F8CECC;strokeColor=#B85450;",
    "opt": "rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#5B6B7B;",
    "runopt": "rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#5B6B7B;dashed=1;",
    "tag": "rounded=1;arcSize=50;whiteSpace=wrap;html=1;fillColor=#EDE3F6;strokeColor=#8E63B5;fontStyle=1;fontSize=16;fontColor=#4A2A7A;",
}


def cell(cid, value, style, x, y, w, h):
    return (
        f'        <mxCell id="{esc(cid)}" value="{esc(value)}" style="{style}" '
        f'vertex="1" parent="1"><mxGeometry x="{x:.0f}" y="{y:.0f}" '
        f'width="{w}" height="{h}" as="geometry"/></mxCell>'
    )


def edge(eid, src, tgt, label="", extra=""):
    style = "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=block;" + extra
    return (
        f'        <mxCell id="{esc(eid)}" value="{esc(label)}" style="{style}" '
        f'edge="1" parent="1" source="{esc(src)}" target="{esc(tgt)}">'
        f'<mxGeometry relative="1" as="geometry"/></mxCell>'
    )


def dtext(text):
    return text.replace("\n", "&#10;")


def build_drawio():
    cells, edges = [], []
    for nid in ORDER:
        typ, text, _ = NODES[nid]
        w, h = GEOM[typ]
        cells.append(cell(nid, dtext(text), DRAWIO_STYLE[typ], CX - w / 2, CY[nid] - h / 2, w, h))
    rail_left = CX - GEOM["proc"][0] / 2
    for nid, label in PHASE_TAGS.items():
        tcx, tcy, tw = tag_geom(label, rail_left, CY[nid])
        cells.append(
            cell(f"{nid}_tag", label, DRAWIO_STYLE["tag"], tcx - tw / 2, tcy - TAG_H / 2, tw, TAG_H)
        )
    for nid in ORDER:
        _, _, branch = NODES[nid]
        if not branch:
            continue
        bid = f"{nid}_term"
        cells.append(
            cell(
                bid,
                dtext(branch["text"]),
                DRAWIO_STYLE[branch["kind"]],
                RIGHT_X,
                CY[nid] - TERM_H / 2,
                TERM_W,
                TERM_H,
            )
        )
        edges.append(edge(f"e_{bid}", nid, bid, branch["cond"]))
        if branch["kind"] == "opt":
            # side-step, not an end state: rejoin the spine at the next node
            edges.append(
                edge(
                    f"e_{bid}_rejoin",
                    bid,
                    NEXT[nid],
                    "",
                    extra="exitX=0.5;exitY=1;exitDx=0;exitDy=0;",
                )
            )
    for a, b in zip(ORDER, ORDER[1:]):
        label = NODES[a][2]["pass"] if NODES[a][2] else ""
        dashed = "dashed=1;" if "runopt" in (NODES[a][0], NODES[b][0]) else ""
        edges.append(edge(f"e_{a}_{b}", a, b, label, extra=dashed))
    body = "\n".join(cells + edges)
    return f"""<mxfile host="app.diagrams.net" type="device">
  <diagram name="9800 WLC upgrade overview" id="c9800-upgrade-overview">
    <mxGraphModel dx="1000" dy="1400" grid="1" gridSize="10" guides="1" tooltips="1"
        connect="1" arrows="1" fold="1" page="1" pageScale="1" math="0" shadow="0">
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
{body}
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
"""


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    docs = os.path.normpath(os.path.join(here, "..", "docs"))
    with open(os.path.join(docs, "9800-overview-flow.svg"), "w") as f:
        f.write(build_svg())
    with open(os.path.join(docs, "9800-overview-flow.drawio"), "w") as f:
        f.write(build_drawio())
    print(f"wrote 9800-overview-flow.svg and 9800-overview-flow.drawio ({len(ORDER)} nodes, {HEIGHT}px tall)")


if __name__ == "__main__":
    main()
