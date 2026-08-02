#!/usr/bin/env python3
"""
threat_excalidraw.py — Render MoiraiCore Threat & Risk assessments as Excalidraw diagrams.

Consumes the OUTPUT of the real threat engine (scripts/threat_mod_toolkit.py:
ThreatRiskEngine.stride_model / .bowtie_analysis) and emits hand-drawn
.excalidraw files viewable in MoiraiCore → Settings → Diagrams.

Why this exists
---------------
The `threat` agent (agents/threat) produces NIST-based STRIDE + bowtie
assessments as markdown only. Those deliverables are diagram-shaped:
  * STRIDE per asset/data flow  -> data-flow diagram with trust boundaries
  * Bowtie analysis             -> bowtie (threats | top event | consequences)
This renderer turns the structured assessment into visual .excalidraw files
using the same full-schema + container-binding + dark-mode approach that the
MoiraiCore Architecture / Task Flow diagrams use (so text centres and shapes render).

Usage
-----
  python3 threat_excalidraw.py --spec spec.json --out ../data/diagrams

spec.json shape (all keys optional per section):
  {
    "stride": {
      "asset_name": "MoiraiCore Platform",
      "asset_type": "Web Application",
      "description": "...",
      "data_flows": [{"name":"User login","source":"User","destination":"server.py",
                      "protocol":"HTTPS","data_type":"Credentials","crosses_boundary":true}],
      "trust_boundaries": ["Browser → API"],
      "entry_points": ["/api/login"]
    },
    "bowtie": {
      "threat_name": "Unauthorised Access",
      "asset": "MoiraiCore API",
      "consequences": ["Data breach","Reputational damage"],
      "preventive_barriers": ["MFA","Rate limiting"],
      "recovery_measures": ["Incident response runbook"],
      "escalation_factors": ["Stolen credential"]
    }
  }

Outputs (into --out):
  threat-stride-<asset>.excalidraw
  threat-bowtie-<asset>.excalidraw

Returns 0 on success.
"""
from __future__ import annotations

import argparse
import datetime
import json
import random
import sys
from pathlib import Path

# Allow running from scripts/ dir
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from threat_mod_toolkit import ThreatRiskEngine
except ImportError:
    ThreatRiskEngine = None  # we still allow raw assessment dicts via --assessment

# ── Excalidraw element helpers (full schema, dark mode, container binding) ──
BG = "#1b1b1b"  # dark canvas default (matches MoiraiCore Diagrams setting)

# STRIDE category -> fill colour (Excalidraw light palette)
STRIDE_FILL = {
    "S": "#ffc9c9",  # Spoofing      — critical/red
    "T": "#ffd8a8",  # Tampering     — warning/orange
    "R": "#fff3bf",  # Repudiation   — decision/yellow
    "I": "#a5d8ff",  # Info Disclosure — input/blue
    "D": "#d0bfff",  # DoS           — special/purple
    "E": "#ffa8a8",  # Elevation     — strong red-orange
}
STRIDE_NAME = {
    "S": "Spoofing", "T": "Tampering", "R": "Repudiation",
    "I": "Info Disclosure", "D": "DoS", "E": "Elevation",
}
NODE_FILL = "#c3fae8"   # storage/data = nodes
EDGE_FILL = "#a5d8ff"   # input = endpoints
EVENT_FILL = "#ffd8a8"  # warning = top event
BARRIER_FILL = "#b2f2bb"  # success = barriers/mitigations
CONS_FILL = "#ffc9c9"   # critical = consequences
THREAT_FILL = "#d0bfff"  # special = threats


def _nonce() -> int:
    return random.randint(1, 2**31)


def _seed() -> int:
    return random.randint(1, 2**31)


class _Idx:
    def __init__(self):
        self.n = 0

    def next(self) -> str:
        self.n += 1
        return "a" + format(self.n, "x")


def _base(el: dict, idx: _Idx) -> dict:
    el.update({
        "angle": 0, "strokeColor": "#1e1e1e", "strokeWidth": 2, "strokeStyle": "solid",
        "roughness": 1, "opacity": 100, "groupIds": [], "frameId": None, "isDeleted": False,
        "version": 1, "versionNonce": _nonce(), "updated": datetime.datetime.now().isoformat() + "Z",
        "link": None, "locked": False, "index": idx.next(),
    })
    return el


def _rect(elements, idx, idn, x, y, w, h, label, fill, fs=16, shape="rectangle", rtype=3):
    tid = "t_" + idn
    elements.append(_base({
        "type": shape, "id": idn, "x": x, "y": y, "width": w, "height": h,
        "backgroundColor": fill, "fillStyle": "solid",
        "roundness": {"type": rtype} if shape in ("rectangle", "diamond") else None,
        "boundElements": [{"id": tid, "type": "text"}],
    }, idx))
    # multi-line label -> join with \n; Excalidraw honours newlines
    _nlines = label.count("\n") + 1
    _th = _nlines * (fs * 1.25) + 6
    elements.append(_base({
        "type": "text", "id": tid, "x": x + 8, "y": y + (h - _th) / 2, "width": w - 16, "height": _th,
        "text": label, "fontSize": fs, "fontFamily": 1, "textAlign": "center",
        "verticalAlign": "middle", "containerId": idn, "originalText": label,
        "lineHeight": 1.25, "baseline": 18, "autoResize": True, "backgroundColor": "transparent",
    }, idx))


def _arrow(elements, idx, idn, x, y, w, h, label=None, dashed=False, color="#1e1e1e"):
    elements.append(_base({
        "type": "arrow", "id": idn, "x": x, "y": y, "width": w, "height": h,
        "points": [[0, 0], [w, h]], "lastCommittedPoint": None,
        "startBinding": None, "endBinding": None, "startArrowhead": None,
        "endArrowhead": "arrow", "polygon": False, "roundness": {"type": 2},
    }, idx))
    if dashed:
        elements[-1]["strokeStyle"] = "dashed"
    if label:
        tid = "t_" + idn
        elements.append(_base({
            "type": "text", "id": tid, "x": x + abs(w) / 2 - 36, "y": y + (h / 2) - 22,
            "width": 72, "height": 18, "text": label, "fontSize": 13, "fontFamily": 1,
            "textAlign": "center", "verticalAlign": "middle", "containerId": None,
            "originalText": label, "lineHeight": 1.25, "baseline": 14, "autoResize": True,
            "backgroundColor": "transparent",
        }, idx))
        elements[-2]["boundElements"] = [{"id": tid, "type": "text"}]


def _title(elements, idx, text, x=80, y=24, fs=24):
    elements.append(_base({
        "type": "text", "id": "title", "x": x, "y": y, "width": 1100, "height": 34,
        "text": text, "fontSize": fs, "fontFamily": 1, "textAlign": "left",
        "verticalAlign": "middle", "containerId": None, "originalText": text,
        "lineHeight": 1.25, "baseline": 22, "autoResize": True, "backgroundColor": "transparent",
    }, idx))


def _heading(elements, idx, text, cx, y, fs=13, color="#e9ecef"):
    """A centered section/column label (no box) to orient the reader.
    Uses a LIGHT stroke colour so it is visible on the dark (#1b1b1b) canvas."""
    w = max(120, len(text) * fs * 0.58)
    el = _base({
        "type": "text", "id": f"h_{abs(hash(text))}_{idx.n}",
        "x": cx - w / 2, "y": y, "width": w, "height": 22,
        "text": text, "fontSize": fs, "fontFamily": 2, "textAlign": "center",
        "verticalAlign": "middle", "containerId": None, "originalText": text,
        "lineHeight": 1.2, "baseline": 16, "autoResize": True,
        "strokeColor": color, "backgroundColor": "transparent",
    }, idx)
    el["strokeColor"] = color  # override _base()'s dark default so it shows on dark canvas
    elements.append(el)


def _wrap(text: str, width: int = 26) -> str:
    """Insert newlines so labels don't overflow narrow boxes."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= width:
            cur = (cur + " " + w).strip()
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines)


# ── STRIDE data-flow diagram ──
def render_stride(result: dict, out_path: Path):
    idx = _Idx()
    elements = []
    asset = result.get("asset_name", "Asset")
    _title(elements, idx, f"STRIDE Threat Model — {asset}")
    summary = result.get("summary", {})

    flows = result.get("flow_threats", [])

    # ── Layout constants (collision-safe) ──
    COL_SRC_X = 80
    BOX_W = 220
    BOX_MIN_H = 64
    GAP_X = 130          # horizontal gap between the 3 columns
    ROW_GAP = 46         # vertical gap between flow rows
    LINE_H = 19          # approx px per wrapped line (font 13)

    def est_h(label):
        n = max(1, label.count("\n") + 1)
        return max(BOX_MIN_H, n * LINE_H + 18)

    # Legend (top-right)
    legend_x = 1040
    legend_lines = ["STRIDE legend"] + [f"{k} {STRIDE_NAME[k]}" for k in "STRIDE"]
    _rect(elements, idx, "legend", legend_x, 100, 260, 30 + len(legend_lines) * 22,
          "\n".join(legend_lines), "#fff3bf", fs=14)

    # ── Column headers (orient the reader) ──
    header_y = 104
    _heading(elements, idx, "SOURCE / ENDPOINT", COL_SRC_X + BOX_W / 2, header_y, fs=18, color="#a5d8ff")
    _heading(elements, idx, "DATA FLOW", fx0 := COL_SRC_X + BOX_W + GAP_X + BOX_W / 2, header_y, fs=18, color="#c3fae8")
    _heading(elements, idx, "DESTINATION", COL_SRC_X + 2 * (BOX_W + GAP_X) + BOX_W / 2, header_y, fs=18, color="#a5d8ff")
    _heading(elements, idx, "Arrows show data/trust flow direction →", 540, header_y + 28, fs=13, color="#868e96")

    # ── Flows: 3 columns (src | flow | dst) ──
    cur_y = 150
    flow_no = 0
    for ft in flows:
        src = ft.get("source") or "Source"
        dst = ft.get("destination") or "Destination"
        fname = ft.get("flow_name", "flow")
        proto = ft.get("protocol", "HTTPS")
        threats = ft.get("threats", [])
        cats = {}
        for t in threats:
            c = t.get("category", "?")
            cats[c] = cats.get(c, 0) + 1
        cat_label = ", ".join(f"{c}×{n}" for c, n in sorted(cats.items())) or "none"
        boundary = "⚠ crosses boundary" if ft.get("crosses_boundary") else ""

        src_lbl = _wrap(src, 18)
        fl_lbl = _wrap(f"{fname}\n({proto})\n[{cat_label}]", 18)
        dst_lbl = _wrap(dst, 18)

        h_src = est_h(src_lbl)
        h_fl = est_h(fl_lbl)
        h_dst = est_h(dst_lbl)
        box_h = max(h_src, h_fl, h_dst)
        # Reserve space for an optional boundary warning (placed just below the row)
        BND_H = 26 + 10
        row_h = box_h + (BND_H if boundary else 0)

        y = cur_y
        _rect(elements, idx, f"src{flow_no}", COL_SRC_X, y, BOX_W, h_src, src_lbl, EDGE_FILL, fs=15)
        fx = COL_SRC_X + BOX_W + GAP_X
        _rect(elements, idx, f"fl{flow_no}", fx, y, BOX_W, h_fl, fl_lbl, NODE_FILL, fs=13)
        dx = fx + BOX_W + GAP_X
        _rect(elements, idx, f"dst{flow_no}", dx, y, BOX_W, h_dst, dst_lbl, EDGE_FILL, fs=15)
        _arrow(elements, idx, f"a{flow_no}a", COL_SRC_X + BOX_W, y + h_src / 2, GAP_X, 0, proto)
        _arrow(elements, idx, f"a{flow_no}b", fx + BOX_W, y + h_fl / 2, GAP_X, 0)

        # Boundary warning: always placed just below the row boxes (reserved space above)
        if boundary:
            bw = 220
            bx = fx
            by = y + box_h + 8
            _rect(elements, idx, f"bd{flow_no}", bx, by, bw, 26, _wrap(boundary, 24), "#ffc9c9", fs=12)

        cur_y += row_h + ROW_GAP
        flow_no += 1

    # ── Trust Boundary Threats: dedicated panel BELOW the flows (no x-collision) ──
    bt = result.get("boundary_threats", [])
    if bt:
        panel_y = cur_y + 10
        panel_w = 1100
        _rect(elements, idx, "btitle", 80, panel_y, panel_w, 34,
              "Trust Boundary Threats", "#ffc9c9", fs=14)
        row_y = panel_y + 44
        bh = BOX_MIN_H
        for i, b in enumerate(bt[:8]):
            lbl = _wrap(f"{b.get('category')} {STRIDE_NAME.get(b.get('category'),'')}: {b.get('boundary')}", 60)
            bh = est_h(lbl)
            # two columns of boundary threats to save vertical space
            col = i % 2
            row = i // 2
            bx = 80 + col * 560
            by = row_y + row * (bh + 12)
            _rect(elements, idx, f"bt{i}", bx, by, 520, bh,
                  lbl, STRIDE_FILL.get(b.get("category"), "#ffffff"), fs=12)
        cur_y = row_y + ((len(bt[:8]) + 1) // 2) * (bh + 12) + 20

    # Footer summary
    foot = (f"Total threats: {summary.get('total_threats','?')} | "
            f"Flows: {summary.get('data_flows_analysed','?')} | "
            f"Boundaries: {summary.get('trust_boundaries','?')} | "
            f"Entry points: {summary.get('entry_points','?')}")
    _rect(elements, idx, "foot", 80, cur_y + 10, 1160, 40, foot, "#c3fae8", fs=13)

    diagram = {
        "type": "excalidraw", "version": 2, "source": "moiraicore-threat",
        "elements": elements, "appState": {"viewBackgroundColor": BG},
        "files": {}, "title": f"STRIDE — {asset}",
        "updated": datetime.datetime.now().isoformat(),
    }
    out_path.write_text(json.dumps(diagram, indent=2))
    return out_path


# ── Bowtie diagram ──
def render_bowtie(result: dict, out_path: Path):
    idx = _Idx()
    elements = []
    threat = result.get("threat_name", "Threat")
    asset = result.get("asset", "Asset")
    _title(elements, idx, f"Bowtie — {threat} ({asset})")

    # ── Column x-positions (collision-safe, all disjoint) ──
    cx = 740            # center of top event
    tx, tw = 60, 240    # threats column       (60 .. 300)
    bx, bw = 330, 180   # barriers column      (330 .. 510)
    # top event                      (cx-190 .. cx+190) -> 550 .. 930
    rx, rw = 980, 320   # consequences column  (980 .. 1300)

    GAP_Y = 18          # vertical gap between stacked boxes
    LINE_H = 17
    HEAD_FS = 18        # heading font size (readable)

    def est_h(label, fs=13):
        n = max(1, label.count("\n") + 1)
        return max(44, n * LINE_H + 14)

    # Top event label (clean threat statement)
    top = result.get("top_event") or threat
    if isinstance(top, str) and top.lower().startswith("loss of") and "controls on" in top.lower():
        top = threat
    top_lbl = _wrap(top, 30)
    top_w = 380
    top_h = est_h(top_lbl, 15)

    # Gather + measure the three vertical stacks
    threats_left = result.get("threats_left", []) or []
    barriers = result.get("preventive_barriers", []) or []
    cons = result.get("consequences_right", []) or []

    def short_barrier(desc):
        # keep barrier text concise so it doesn't wrap to 7 lines
        t = desc.strip()
        if len(t) > 34:
            t = t[:32].rstrip() + "…"
        return "✔ " + t

    t_h = [est_h(_wrap(t.get("description", "threat"), 22), 13) for t in threats_left]
    b_h = [est_h(short_barrier(b.get("description", "")), 12) for b in barriers]
    c_h = [est_h(_wrap(c.get("description", "consequence"), 26), 13) for c in cons]

    def stack_h(hs):
        if not hs:
            return 44
        return sum(hs) + GAP_Y * (len(hs) - 1)

    threats_total = stack_h(t_h)
    barriers_total = stack_h(b_h)
    cons_total = stack_h(c_h)
    max_stack = max(threats_total, barriers_total, cons_total)

    # Vertical centre of the stacks — leave room for the column headers on top
    top_of_stacks = 90
    cy = top_of_stacks + max_stack / 2

    # ── Top event (center) ──
    _rect(elements, idx, "topevent", cx - top_w / 2, cy - top_h / 2, top_w, top_h, top_lbl, EVENT_FILL, fs=15)
    _heading(elements, idx, "TOP EVENT", cx, top_of_stacks - 34, fs=HEAD_FS, color="#ffd8a8")

    # ── THREATS (left) ──
    _heading(elements, idx, "THREATS", tx + tw / 2, top_of_stacks - 34, fs=HEAD_FS, color="#d0bfff")
    yy = top_of_stacks
    for i, t in enumerate(threats_left):
        lbl = _wrap(t.get("description", "threat"), 22)
        h = t_h[i]
        _rect(elements, idx, f"tl{i}", tx, yy, tw, h, lbl, THREAT_FILL, fs=13)
        _arrow(elements, idx, f"at{i}", tx + tw, yy + h / 2, bx - (tx + tw), 0)
        yy += h + GAP_Y

    # ── PREVENTIVE BARRIERS (center-left) ──
    _heading(elements, idx, "PREVENTIVE BARRIERS", bx + bw / 2, top_of_stacks - 34, fs=HEAD_FS, color="#b2f2bb")
    yy = top_of_stacks
    for i, b in enumerate(barriers):
        lbl = short_barrier(b.get("description", ""))
        h = b_h[i]
        _rect(elements, idx, f"pb{i}", bx, yy, bw, h, lbl, BARRIER_FILL, fs=12)
        yy += h + GAP_Y

    # ── CONSEQUENCES (right) ──
    _heading(elements, idx, "CONSEQUENCES", rx + rw / 2, top_of_stacks - 34, fs=HEAD_FS, color="#ffc9c9")
    yy = top_of_stacks
    for i, c in enumerate(cons):
        lbl = _wrap(c.get("description", "consequence"), 26)
        h = c_h[i]
        _rect(elements, idx, f"co{i}", rx, yy, rw, h, lbl, CONS_FILL, fs=13)
        _arrow(elements, idx, f"ac{i}", cx + 200, yy + h / 2, rx - (cx + 200), 0)
        yy += h + GAP_Y

    # ── RECOVERY MEASURES (bottom row) ──
    rec = result.get("recovery_measures", []) or []
    if rec:
        rm_w = 250
        rm_gap = 22
        total_w = len(rec) * rm_w + (len(rec) - 1) * rm_gap
        start_x = cx - total_w / 2
        rm_y = cy + max_stack / 2 + 90
        _heading(elements, idx, "RECOVERY MEASURES", cx, rm_y - 34, fs=HEAD_FS, color="#b2f2bb")
        for i, m in enumerate(rec):
            lbl = _wrap(m.get("description", ""), 22)
            h = est_h(lbl, 12)
            _rect(elements, idx, f"rm{i}", start_x + i * (rm_w + rm_gap), rm_y, rm_w, h,
                  lbl, BARRIER_FILL, fs=12)

    diagram = {
        "type": "excalidraw", "version": 2, "source": "moiraicore-threat",
        "elements": elements, "appState": {"viewBackgroundColor": BG},
        "files": {}, "title": f"Bowtie — {threat}",
        "updated": datetime.datetime.now().isoformat(),
    }
    out_path.write_text(json.dumps(diagram, indent=2))
    return out_path


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in (s or "asset")).strip("-").lower()


def main():
    ap = argparse.ArgumentParser(description="Render MoiraiCore threat assessments as Excalidraw diagrams.")
    ap.add_argument("--spec", help="JSON spec with 'stride' and/or 'bowtie' sections")
    ap.add_argument("--assessment", help="Pre-computed assessment JSON (keys: stride, bowtie)")
    ap.add_argument("--out", default=str(HERE.parent / "data" / "diagrams"),
                    help="Output directory for .excalidraw files")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    spec = {}
    if args.spec:
        spec = json.loads(Path(args.spec).read_text())
    elif args.assessment:
        spec = json.loads(Path(args.assessment).read_text())
    else:
        print("ERROR: provide --spec or --assessment", file=sys.stderr)
        return 2

    wrote = []
    engine = ThreatRiskEngine() if ThreatRiskEngine else None

    if "stride" in spec:
        s = spec["stride"]
        if engine:
            result = engine.stride_model(
                asset_name=s.get("asset_name", "Asset"),
                asset_type=s.get("asset_type", "Web Application"),
                description=s.get("description", ""),
                data_flows=s.get("data_flows"),
                trust_boundaries=s.get("trust_boundaries"),
                entry_points=s.get("entry_points"),
            )
        else:
            result = s  # assume pre-computed
        p = out_dir / f"threat-stride-{_slug(s.get('asset_name','asset'))}.excalidraw"
        render_stride(result, p)
        wrote.append(p)

    if "bowtie" in spec:
        b = spec["bowtie"]
        if engine:
            result = engine.bowtie_analysis(
                threat_name=b.get("threat_name", "Threat"),
                asset=b.get("asset", "Asset"),
                consequences=b.get("consequences"),
                preventive_barriers=b.get("preventive_barriers"),
                recovery_measures=b.get("recovery_measures"),
                escalation_factors=b.get("escalation_factors"),
            )
        else:
            result = b
        p = out_dir / f"threat-bowtie-{_slug(b.get('asset','asset'))}.excalidraw"
        render_bowtie(result, p)
        wrote.append(p)

    for p in wrote:
        print(f"wrote {p} ({len(json.loads(p.read_text()).get('elements', []))} elements)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
