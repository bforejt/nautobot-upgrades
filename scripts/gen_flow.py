#!/usr/bin/env python3
"""Generate docs/upgrade-flow.drawio (editable) and docs/upgrade-flow.svg (image)
from one node/edge model that mirrors IOSXEUpgrade._upgrade_device()."""

import html
import os

CX = 400          # spine center x
PITCH = 96        # row center-to-center
TOP = 56          # first row center y

# Geometry per node type: (width, height)
GEOM = {
    "start": (300, 50),
    "end":   (320, 54),
    "proc":  (300, 56),
    "dec":   (250, 84),
}
ABORT_W, ABORT_H = 330, 66
WARN_W, WARN_H = 220, 60
OKR_W, OKR_H = 330, 56
RIGHT_X = 600     # left edge of right-column boxes
LEFT_RX = 250     # right edge of left-column (warn) boxes

# spine: ordered. Each: id, type, text, and optional branches:
#   abort=(condlabel, reasontext)       -> red box on the right
#   warn=(condlabel, notetext)          -> amber box on the left (flow continues)
#   okright=(condlabel, terminaltext)   -> green terminal on the right
#   passlabel="..."                     -> label on the downward (continue) edge
#   bypass=(condlabel, note)            -> edge that skips the NEXT node down to the one after
SPINE = [
    ("start", "start", "FOR EACH selected device", {}),
    ("host", "proc", "Resolve mgmt host\n(primary_ip4 / primary_ip)", {}),
    ("d_ip", "dec", "Primary IP set?",
     {"abort": ("No", "No primary IP set")}),
    ("cred", "proc", "Resolve credentials\n(job override OR device Secrets Group)", {}),
    ("d_cred", "dec", "Secrets group +\nusername & password?",
     {"abort": ("No", "No Secrets Group / secret missing")}),
    ("reach", "proc", "GET device-system-data\n(auth + reachability)", {}),
    ("d_reach", "dec", "Reachable &\nauthenticated?",
     {"abort": ("No", "HTTP 401 auth / 403 privilege /\nunreachable (RESTCONF disabled?)")}),
    ("curr", "proc", "Read current version\n(from the reachability response)", {}),
    ("d_target", "dec", "Already on\ntarget version?",
     {"okright": ("Yes", "Already on target → no-op if committed;\nelse install commit (commit-to-be-safe)"),
      "passlabel": "No"}),
    ("d_floor", "dec", "Version parses\n& ≥ 17.12.1?",
     {"abort": ("No", "Unknown version, or below the\n17.12.1 support floor"),
      "passlabel": "Yes"}),
    ("mode", "proc", "Read oper-state boot-mode\n(install-oper, all members)", {}),
    ("d_mode", "dec", "All members\nINSTALL mode?",
     {"abort": ("bundle / bad",
                "BUNDLE, unrecognized mode, or\ninstall-oper unreadable (fail-closed)"),
      "warn": ("bundle-derived /\nopt-in",
               "install-bundle, or absent/'unknown'\nmode + assume_install_mode → proceed"),
      "passlabel": "Yes"}),
    ("img", "proc", "Resolve image (device override →\ndevice-type map → default)", {}),
    ("d_img", "dec", "Image has filename\n& download URL?",
     {"abort": ("No", "No compatible image, or missing\nfilename / download URL"),
      "passlabel": "Yes"}),
    ("space", "proc", "Read free space\n(q-filesystem, exact flash: match)", {}),
    ("d_space", "dec", "Free ≥ image × 2\n(or ≥ 2 GB if size unknown)?",
     {"abort": ("No", "Free space unconfirmed or\ninsufficient"), "passlabel": "Yes"}),
    ("d_dry", "dec", "Dry-run?",
     {"okright": ("Yes", "DONE: DRY-RUN — pre-flight\npassed, no changes made"),
      "passlabel": "No"}),
    ("copy", "proc", "Async express copy (xcopy) + poll size:\nprogress %, stall watch (skip if file present)", {}),
    ("d_size", "dec", "Transfer complete\n& size matches?",
     {"abort": ("stall / timeout /\noversize", "No progress, timeout, or file\nlarger than expected — abort"),
      "warn": ("size unknown", "No expected size → settle-detect,\nrely on install add signature"),
      "passlabel": "match"}),
    ("add", "proc", "install add (poll until staged: pending/\nadded or beyond; warn if unconfirmed)", {}),
    ("act", "proc", "install activate (explicitly non-ISSU,\nby version; log RPC response)", {}),
    ("d_act", "dec", "Activation started?\n(state moves or device drops)",
     {"abort": ("No", "State never moved & device still up —\nengine rejected activate; device unchanged"),
      "passlabel": "Yes"}),
    ("waitc", "proc", "Wait for reload; poll booted version\nuntil stable (2 consecutive matches)", {}),
    ("d_confirm", "dec", "Target version\nstably confirmed?",
     {"abort": ("No", "Not confirmed before timeout — NOT\ncommitted; auto-rollback should revert"),
      "passlabel": "Yes"}),
    ("rollbackchk", "proc", "Report auto-rollback timer status\n(informational; commit follows)", {}),
    ("commit", "proc", "install commit → poll until confirmed\ncommitted (scoped to target version)", {}),
    ("d_commit", "dec", "Commit\nsucceeded?",
     {"abort": ("No", "Commit failed — ACTIVATED but NOT\ncommitted; manual intervention / re-run"),
      "passlabel": "Yes"}),
    ("sync", "proc", "Sync Nautobot software_version\n(warn on fail; already committed)", {}),
    ("d_remove", "dec", "remove_inactive\nenabled?",
     {"passlabel": "Yes", "bypass": ("No", "skip")}),
    ("remove", "proc", "install remove inactive\n(warn on fail)", {}),
    ("ok", "end", "DONE: UPGRADED & COMMITTED ✓", {}),
]

# assign y-centers
CY = {}
for i, (nid, *_rest) in enumerate(SPINE):
    CY[nid] = TOP + i * PITCH

NODES = {nid: (typ, text, opts) for nid, typ, text, opts in SPINE}
ORDER = [nid for nid, *_ in SPINE]

WIDTH = 980
HEIGHT = CY[ORDER[-1]] + 80

LEGEND = ("Legend:  diamonds = decisions  ·  red = ABORT this device "
          "(logs the reason and continues with the next selected device)  ·  "
          "amber = warn & continue  ·  green = successful end state.")

# ---------------------------------------------------------------- SVG output --


def esc(s):
    return html.escape(s, quote=True)


def lines(text):
    return text.split("\n")


def svg_text(cx, cy, text, size=12, bold=False, color="#1a1a1a", anchor="middle"):
    ls = lines(text)
    lh = size + 3
    start = cy - (len(ls) - 1) * lh / 2
    weight = ' font-weight="bold"' if bold else ""
    out = [f'<text x="{cx}" y="{start:.0f}" text-anchor="{anchor}" '
           f'font-family="Helvetica,Arial,sans-serif" font-size="{size}"{weight} '
           f'fill="{color}">']
    for k, ln in enumerate(ls):
        dy = 0 if k == 0 else lh
        out.append(f'<tspan x="{cx}" dy="{dy:.0f}">{esc(ln)}</tspan>')
    out.append("</text>")
    return "".join(out)


def rect(cx, cy, w, h, fill, stroke, rx=6):
    return (f'<rect x="{cx-w/2:.0f}" y="{cy-h/2:.0f}" width="{w}" height="{h}" '
            f'rx="{rx}" ry="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')


def diamond(cx, cy, w, h, fill, stroke):
    pts = f"{cx},{cy-h/2} {cx+w/2},{cy} {cx},{cy+h/2} {cx-w/2},{cy}"
    return f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'


def arrow(x1, y1, x2, y2, dashed=False, color="#555"):
    dash = ' stroke-dasharray="5,4"' if dashed else ""
    return (f'<path d="M {x1:.0f} {y1:.0f} L {x2:.0f} {y2:.0f}" fill="none" '
            f'stroke="{color}" stroke-width="1.5"{dash} marker-end="url(#arrow)"/>')


def elbow(points, dashed=False, color="#555"):
    d = "M " + " L ".join(f"{x:.0f} {y:.0f}" for x, y in points)
    dash = ' stroke-dasharray="5,4"' if dashed else ""
    return (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.5"{dash} '
            f'marker-end="url(#arrow)"/>')


def edge_label(x, y, text, color="#333"):
    w = max(len(text) * 6.5 + 8, 18)
    return (f'<rect x="{x-w/2:.0f}" y="{y-9:.0f}" width="{w:.0f}" height="18" rx="3" '
            f'fill="#ffffff" fill-opacity="0.85" stroke="none"/>'
            + svg_text(x, y + 4, text, size=11, color=color))


FILLS = {
    "start": ("#DAE8FC", "#6C8EBF"),
    "end": ("#D5E8D4", "#2E7D32"),
    "proc": ("#FFFFFF", "#5B6B7B"),
    "dec": ("#E8EEF6", "#3C6CA8"),
    "abort": ("#F8CECC", "#B85450"),
    "warn": ("#FFE6CC", "#D79B00"),
    "okr": ("#D5E8D4", "#2E7D32"),
}


def build_svg():
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" '
         f'width="{WIDTH}" height="{HEIGHT}" font-family="Helvetica,Arial,sans-serif">']
    s.append('<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" '
             'refY="3" orient="auto" markerUnits="strokeWidth">'
             '<path d="M0,0 L8,3 L0,6 z" fill="#555"/></marker></defs>')
    s.append(f'<rect x="0" y="0" width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>')
    s.append('<text x="20" y="28" font-size="18" font-weight="bold" fill="#111">'
             'Cisco IOS-XE Upgrade (RESTCONF) — per-device flow</text>')
    # legend box top-right
    s.append(rect(770, 86, 400, 96, "#fbfbfb", "#bbb", rx=6))
    s.append(svg_text(770, 86, LEGEND.replace("  ·  ", "\n· "),
                      size=10.5, color="#333"))

    # spine down edges (and pass labels)
    for a, b in zip(ORDER, ORDER[1:]):
        ta, _, _ = NODES[a]
        tb, _, _ = NODES[b]
        ha = GEOM[ta][1]
        hb = GEOM[tb][1]
        y1 = CY[a] + ha / 2
        y2 = CY[b] - hb / 2
        s.append(arrow(CX, y1, CX, y2))
        pl = NODES[a][2].get("passlabel")
        if pl:
            s.append(edge_label(CX + 16, (y1 + y2) / 2, pl))

    # special: d_remove bypass (No) skips 'remove' -> 'ok'
    rem_opts = NODES["d_remove"][2]
    if "bypass" in rem_opts:
        cond, _ = rem_opts["bypass"]
        yx = 200
        s.append(elbow([(CX - GEOM["dec"][0] / 2, CY["d_remove"]),
                        (yx, CY["d_remove"]),
                        (yx, CY["ok"]),
                        (CX - GEOM["end"][0] / 2, CY["ok"])]))
        s.append(edge_label(yx + 26, (CY["d_remove"] + CY["ok"]) / 2, cond))

    # branch boxes (abort/warn/okright)
    for nid in ORDER:
        typ, _, opts = NODES[nid]
        cy = CY[nid]
        dw = GEOM["dec"][0]
        if "abort" in opts:
            cond, reason = opts["abort"]
            bx = RIGHT_X + ABORT_W / 2
            s.append(arrow(CX + dw / 2, cy, RIGHT_X, cy, color="#B85450"))
            s.append(edge_label((CX + dw / 2 + RIGHT_X) / 2, cy - 10, cond, color="#B85450"))
            s.append(rect(bx, cy, ABORT_W, ABORT_H, *FILLS["abort"]))
            s.append(svg_text(bx, cy - 9, "ABORT", size=11, bold=True, color="#8a1f1f"))
            s.append(svg_text(bx, cy + 9, reason, size=10.5, color="#5b1414"))
        if "okright" in opts:
            cond, term = opts["okright"]
            bx = RIGHT_X + OKR_W / 2
            s.append(arrow(CX + dw / 2, cy, RIGHT_X, cy, color="#2E7D32"))
            s.append(edge_label((CX + dw / 2 + RIGHT_X) / 2, cy - 10, cond, color="#2E7D32"))
            s.append(rect(bx, cy, OKR_W, OKR_H, *FILLS["okr"], rx=22))
            s.append(svg_text(bx, cy, term, size=11, bold=True, color="#1b5e20"))
        if "warn" in opts:
            cond, note = opts["warn"]
            bx = LEFT_RX - WARN_W / 2
            s.append(arrow(CX - dw / 2, cy, LEFT_RX, cy, dashed=True, color="#D79B00"))
            s.append(edge_label((CX - dw / 2 + LEFT_RX) / 2, cy - 10, cond, color="#B7791f"))
            s.append(rect(bx, cy, WARN_W, WARN_H, *FILLS["warn"]))
            s.append(svg_text(bx, cy, note, size=10.5, color="#7a4f00"))
            # warn-and-continue: rejoin the main flow at the next spine node
            nxt = ORDER[ORDER.index(nid) + 1]
            ny = CY[nxt]
            nx_left = CX - GEOM[NODES[nxt][0]][0] / 2
            s.append(elbow([(bx, cy + WARN_H / 2), (bx, ny), (nx_left, ny)],
                           dashed=True, color="#D79B00"))
            s.append(edge_label((bx + nx_left) / 2, ny - 10, "continue", color="#B7791f"))

    # spine nodes (draw last so they sit on top of edges)
    for nid in ORDER:
        typ, text, _ = NODES[nid]
        cy = CY[nid]
        w, h = GEOM[typ]
        if typ == "dec":
            s.append(diamond(CX, cy, w, h, *FILLS["dec"]))
            s.append(svg_text(CX, cy, text, size=11, bold=True, color="#13335c"))
        elif typ in ("start", "end"):
            s.append(rect(CX, cy, w, h, *FILLS[typ], rx=24))
            s.append(svg_text(CX, cy, text, size=12, bold=True,
                              color="#1b3a6b" if typ == "start" else "#14532d"))
        else:
            s.append(rect(CX, cy, w, h, *FILLS["proc"], rx=6))
            s.append(svg_text(CX, cy, text, size=11, color="#1a1a1a"))

    s.append("</svg>")
    return "\n".join(s)


# ------------------------------------------------------------- drawio output --

DRAWIO_STYLE = {
    "start": "rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#DAE8FC;strokeColor=#6C8EBF;",
    "end": "rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#D5E8D4;strokeColor=#2E7D32;fontStyle=1;",
    "proc": "rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#5B6B7B;",
    "dec": "rhombus;whiteSpace=wrap;html=1;fillColor=#E8EEF6;strokeColor=#3C6CA8;fontStyle=1;",
    "abort": "rounded=1;whiteSpace=wrap;html=1;fillColor=#F8CECC;strokeColor=#B85450;",
    "warn": "rounded=1;whiteSpace=wrap;html=1;fillColor=#FFE6CC;strokeColor=#D79B00;",
    "okr": "rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#D5E8D4;strokeColor=#2E7D32;fontStyle=1;",
}


def cell(cid, value, style, x, y, w, h):
    return (f'        <mxCell id="{esc(cid)}" value="{esc(value)}" style="{style}" '
            f'vertex="1" parent="1"><mxGeometry x="{x:.0f}" y="{y:.0f}" '
            f'width="{w}" height="{h}" as="geometry"/></mxCell>')


def edge(eid, src, tgt, label="", dashed=False):
    style = "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=block;"
    if dashed:
        style += "dashed=1;"
    return (f'        <mxCell id="{esc(eid)}" value="{esc(label)}" style="{style}" '
            f'edge="1" parent="1" source="{esc(src)}" target="{esc(tgt)}">'
            f'<mxGeometry relative="1" as="geometry"/></mxCell>')


def dtext(text):
    return text.replace("\n", "&#10;")


def build_drawio():
    cells = []
    edges = []
    # spine vertices
    for nid in ORDER:
        typ, text, _ = NODES[nid]
        w, h = GEOM[typ]
        cy = CY[nid]
        cells.append(cell(nid, dtext(text), DRAWIO_STYLE[typ], CX - w / 2, cy - h / 2, w, h))
    # branch vertices + edges
    for nid in ORDER:
        typ, _, opts = NODES[nid]
        cy = CY[nid]
        if "abort" in opts:
            cond, reason = opts["abort"]
            bid = f"{nid}_abort"
            cells.append(cell(bid, "ABORT&#10;" + dtext(reason), DRAWIO_STYLE["abort"],
                              RIGHT_X, cy - ABORT_H / 2, ABORT_W, ABORT_H))
            edges.append(edge(f"e_{bid}", nid, bid, cond))
        if "okright" in opts:
            cond, term = opts["okright"]
            bid = f"{nid}_ok"
            cells.append(cell(bid, dtext(term), DRAWIO_STYLE["okr"],
                              RIGHT_X, cy - OKR_H / 2, OKR_W, OKR_H))
            edges.append(edge(f"e_{bid}", nid, bid, cond))
        if "warn" in opts:
            cond, note = opts["warn"]
            bid = f"{nid}_warn"
            cells.append(cell(bid, dtext(note), DRAWIO_STYLE["warn"],
                              LEFT_RX - WARN_W, cy - WARN_H / 2, WARN_W, WARN_H))
            edges.append(edge(f"e_{bid}", nid, bid, cond))
            nxt = ORDER[ORDER.index(nid) + 1]
            edges.append(edge(f"e_{bid}_join", bid, nxt, "continue", dashed=True))
    # spine edges
    for a, b in zip(ORDER, ORDER[1:]):
        if a == "d_remove" and b == "remove":
            edges.append(edge("e_remove_yes", a, b, "Yes"))
            continue
        pl = NODES[a][2].get("passlabel", "")
        edges.append(edge(f"e_{a}_{b}", a, b, pl))
    # d_remove bypass (No) -> ok
    edges.append(edge("e_remove_no", "d_remove", "ok", "No"))

    body = "\n".join(cells + edges)
    return f'''<mxfile host="app.diagrams.net" type="device">
  <diagram name="IOS-XE upgrade flow" id="iosxe-upgrade-flow">
    <mxGraphModel dx="1000" dy="2000" grid="1" gridSize="10" guides="1" tooltips="1"
        connect="1" arrows="1" fold="1" page="1" pageScale="1" math="0" shadow="0">
      <root>
        <mxCell id="0"/>
        <mxCell id="1" parent="0"/>
{body}
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
'''


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    docs = os.path.normpath(os.path.join(here, "..", "docs"))
    with open(os.path.join(docs, "upgrade-flow.svg"), "w") as f:
        f.write(build_svg())
    with open(os.path.join(docs, "upgrade-flow.drawio"), "w") as f:
        f.write(build_drawio())
    print(f"wrote upgrade-flow.svg and upgrade-flow.drawio ({len(ORDER)} spine nodes, "
          f"{HEIGHT}px tall)")


if __name__ == "__main__":
    main()
