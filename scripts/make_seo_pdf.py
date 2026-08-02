#!/usr/bin/env python3
"""
Generate a professional SEO Audit PDF report from the MoiraiCore SEO Toolkit
`full_audit` JSON output.

Usage:
    python make_seo_pdf.py <audit.json> <output.pdf>

Renders a dark, MoiraiCore-branded HTML report and prints it to PDF using
headless Chromium (Playwright) — the same rendering engine the MoiraiCore
platform uses, so the output matches the dashboard's visual language.
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# ── MoiraiCore design tokens (must match dashboard/index.html) ──
BG = "#0a0a0f"
SURFACE = "#14141c"
SURFACE2 = "#1c1c28"
BORDER = "#2a2a3a"
ACCENT = "#7c5bf5"
TEXT = "#e8e8f0"
DIM = "#8a8aa0"
GREEN = "#4ade80"
YELLOW = "#fbbf24"
ORANGE = "#fb923c"
RED = "#f87171"

GRADE_COLORS = {
    "A+": GREEN, "A": GREEN, "B": YELLOW, "C": ORANGE,
    "D": ORANGE, "F": RED, "N/A": DIM,
}
PRIORITY_COLORS = {"high": RED, "medium": YELLOW, "low": ACCENT}

# ── Concrete fixes to lift D -> B (derived from this audit) ──
FIXES = [
    {"id": 1, "priority": "High", "issue": "Thin content (66 words)",
     "fix": "Expand homepage copy to 300–600+ words of unique, service-rich text (rooms, bistro menu, bar, functions, location/transport, trading hours). Target 800+ for a B.",
     "impact": "+10–15 score", "effort": "M"},
    {"id": 2, "priority": "High", "issue": "Missing meta description",
     "fix": 'Add &lt;meta name="description" content="Crown Hotel Parramatta — bright bars, bistro &amp; functions in the heart of Parramatta. Book your table, event or function today."&gt; (120–160 chars).',
     "impact": "+10 score", "effort": "S"},
    {"id": 3, "priority": "Medium", "issue": "Title too long (96 chars)",
     "fix": 'Shorten &lt;title&gt; to 50–60 chars, e.g. "Crown Hotel Parramatta | Bistro, Bar &amp; Functions". Keep brand + primary keyword.',
     "impact": "+7 (title length)", "effort": "S"},
    {"id": 4, "priority": "Medium", "issue": "No schema / JSON-LD",
     "fix": "Add LocalBusiness + Restaurant JSON-LD with name, address, phone (02) 9633 2600, cuisine, openingHours, geo. Enables rich snippets.",
     "impact": "+4 score", "effort": "M"},
    {"id": 5, "priority": "Medium", "issue": "2 links with no anchor text",
     "fix": 'Give the 2 empty anchors descriptive text (e.g. "Book a function", "View menu") instead of (no text).',
     "impact": "accessibility + minor SEO", "effort": "S"},
    {"id": 6, "priority": "Low", "issue": "No Open Graph tags",
     "fix": "Add og:title, og:description, og:image, og:type=website for share-previews on social.",
     "impact": "+3 score", "effort": "S"},
    {"id": 7, "priority": "Low", "issue": "No external / authority links",
     "fix": "Link to 1–2 authoritative external sources (e.g. Parramatta council, transport info) for credibility.",
     "impact": "+3 score", "effort": "S"},
    {"id": 8, "priority": "Low", "issue": "Keyword stuffing risk (parramatta 9.5%)",
     "fix": "Diversify copy so primary keyword density sits in 1–2.5% range; use synonyms (Parramatta CBD, Western Sydney).",
     "impact": "avoids penalty + ranking", "effort": "S"},
]


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_html(data):
    score = data.get("score")
    grade = data.get("grade", "N/A")
    summary = data.get("summary", {})
    recs = data.get("recommendations", [])
    counts = data.get("recommendation_counts", {})
    kw = data.get("top_keywords", [])
    content = data.get("content_analysis", {})
    back = data.get("backlink_analysis", {})
    readability = content.get("readability", {})
    structure = content.get("structure", {})
    images = content.get("images", {})
    links = content.get("links", {})
    url = data.get("url", "")
    audited = data.get("audited_at", "")
    audit_time = data.get("audit_time", 0)

    grade_color = GRADE_COLORS.get(grade, DIM)
    score_display = "—" if score is None else score

    # Score ring (conic-gradient circle)
    if score is not None:
        ring = f"""
        <div class="ring" style="background:conic-gradient({grade_color} {score*3.6}deg,{SURFACE2} 0deg);">
          <div class="ring-inner">
            <div class="score-num" style="color:{grade_color}">{score}</div>
            <div class="score-grade">Grade {esc(grade)}</div>
          </div>
        </div>"""
    else:
        ring = f"""
        <div class="ring" style="background:{SURFACE2};">
          <div class="ring-inner">
            <div class="score-num" style="color:{grade_color}">N/A</div>
          </div>
        </div>"""

    # Stat tiles
    def tile(label, value, ok=True, warn=False):
        c = GREEN if ok else (RED if warn else TEXT)
        return f"""<div class="tile"><div class="tile-val" style="color:{c}">{esc(value)}</div><div class="tile-label">{esc(label)}</div></div>"""

    tiles = "".join([
        tile("Word Count", summary.get("word_count", 0),
             summary.get("word_count", 0) >= 300, summary.get("word_count", 0) < 300),
        tile("Title Length", f'{summary.get("title_length",0)} ch',
             summary.get("title_length", 0) <= 60),
        tile("Meta Desc", "Present" if summary.get("meta_description_length",0) else "Missing",
             bool(summary.get("meta_description_length",0)), not bool(summary.get("meta_description_length",0))),
        tile("Readability", f'{summary.get("readability",0)}', summary.get("readability",0) >= 40),
        tile("Images + Alt", f'{images.get("total",0)} / {images.get("with_alt",0)}',
             images.get("without_alt",0) == 0),
        tile("Schema", "Yes" if summary.get("has_schema") else "No", summary.get("has_schema")),
        tile("Canonical", "Yes" if summary.get("has_canonical") else "No", summary.get("has_canonical")),
        tile("OG Tags", "Yes" if summary.get("has_og_tags") else "No", summary.get("has_og_tags")),
        tile("Internal Links", summary.get("internal_links",0), summary.get("internal_links",0) >= 2),
        tile("External Links", summary.get("external_links",0), summary.get("external_links",0) >= 1,
             summary.get("external_links",0) == 0),
    ])

    # Keyword bars
    max_density = max([k.get("density", 0) for k in kw], default=1) or 1
    kw_rows = ""
    for k in kw:
        pct = (k.get("density", 0) / max_density) * 100
        kw_rows += f"""
        <div class="kw-row">
          <div class="kw-term">{esc(k.get('term',''))}</div>
          <div class="kw-bar"><div class="kw-fill" style="width:{pct:.0f}%"></div></div>
          <div class="kw-meta">{k.get('count',0)}× · {k.get('density',0)}%</div>
        </div>"""

    # Recommendations
    rec_rows = ""
    for r in recs:
        pr = r.get("priority", "low")
        c = PRIORITY_COLORS.get(pr, DIM)
        badge = f'<span class="pill" style="background:{c}22;color:{c};border-color:{c}55">{esc(pr.upper())}</span>'
        rec_rows += f'<div class="rec">{badge}<div class="rec-text">{esc(r.get("text",""))}</div></div>'

    # Fixes checklist
    fix_rows = ""
    for f in FIXES:
        pr = f["priority"].lower()
        c = PRIORITY_COLORS.get(pr, DIM)
        checkbox = f'<span class="chk" style="border-color:{c}">☐</span>'
        badge = f'<span class="pill" style="background:{c}22;color:{c};border-color:{c}55">{esc(f["priority"].upper())}</span>'
        fix_rows += f"""
        <div class="fix">
          {checkbox}
          <div class="fix-body">
            <div class="fix-head">
              <span class="fix-issue">#{f['id']} · {esc(f['issue'])}</span>
              {badge}
              <span class="fix-effort">effort: {esc(f['effort'])}</span>
            </div>
            <div class="fix-text">{f['fix']}</div>
            <div class="fix-impact">Impact: <b style="color:{GREEN}">{esc(f['impact'])}</b></div>
          </div>
        </div>"""

    # Anchor texts
    anchors = back.get("anchor_texts", [])
    anchor_rows = ""
    for a in anchors[:12]:
        anchor_rows += f'<div class="anchor"><span class="anchor-text">{esc(a.get("text",""))}</span><span class="anchor-count">{a.get("count",0)}</span></div>'

    # Projected score if all fixes applied. We must not double-count categories
    # the audit already partially awards, so use conservative, non-overlapping gains.
    base = score if isinstance(score, int) else 0
    # Gains are independent of current partial credit in this engine:
    #   content depth (+15, currently 0), meta desc (+10, currently 0),
    #   title length (+7, currently 8/15 already), schema (+4, 0),
    #   OG/social (+3, 0), external links (+3, 0)  -> ~ +42 gross, but title
    #   only yields the remaining +7, so net realistic lift is ~ +34.
    projected_lift = 34
    projected_low = min(base + projected_lift, 100)
    projected_high = min(base + 42, 100)
    proj_label = f"{projected_low}–{projected_high}"

    title = summary.get("title", "") or "(untitled)"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 0; }}
* {{ box-sizing: border-box; }}
html, body {{ background: {BG}; }}
body {{ margin:0; padding:14mm 14mm 16mm; background:{BG}; color:{TEXT};
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 11px; line-height: 1.5; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
.page-break {{ page-break-before: always; }}
h2 {{ font-size:13px; letter-spacing:.6px; text-transform:uppercase; color:{ACCENT};
  margin:0 0 12px; padding-bottom:6px; border-bottom:1px solid {BORDER};
  page-break-after: avoid; }}
h2:not(:first-of-type) {{ margin-top:26px; }}

.header {{ display:flex; align-items:center; justify-content:space-between;
  padding:14px 18px; background:{SURFACE}; border:1px solid {BORDER}; border-radius:12px;
  page-break-inside: avoid; }}
.brand {{ display:flex; align-items:center; gap:10px; }}
.brand-mark {{ width:28px; height:28px; border-radius:8px;
  background:linear-gradient(135deg,{ACCENT},#a78bfa); display:flex; align-items:center; justify-content:center;
  font-weight:800; color:#fff; font-size:14px; }}
.brand-name {{ font-weight:700; font-size:14px; letter-spacing:.3px; }}
.brand-sub {{ font-size:10px; color:{DIM}; }}
.tag {{ font-size:10px; color:{DIM}; text-align:right; }}
.tag b {{ color:{TEXT}; font-weight:600; }}

.score-grid {{ display:flex; gap:22px; align-items:center; margin:18px 0 4px;
  page-break-inside: avoid; }}
.ring {{ width:118px; height:118px; border-radius:50%; display:flex; align-items:center; justify-content:center; flex:0 0 auto; }}
.ring-inner {{ width:92px; height:92px; border-radius:50%; background:{SURFACE};
  display:flex; flex-direction:column; align-items:center; justify-content:center; }}
.score-num {{ font-size:34px; font-weight:800; line-height:1; }}
.score-grade {{ font-size:10px; color:{DIM}; margin-top:4px; }}
.score-meta {{ flex:1; min-width:0; }}
.score-meta .url {{ font-size:13px; font-weight:600; color:{TEXT}; word-break:break-all; }}
.score-meta .title {{ font-size:11px; color:{DIM}; margin-top:6px; }}
.score-meta .counts {{ margin-top:8px; font-size:10px; color:{DIM}; }}

.tiles {{ display:grid; grid-template-columns:repeat(5,1fr); gap:8px; margin-top:18px;
  page-break-inside: avoid; }}
.tile {{ background:{SURFACE}; border:1px solid {BORDER}; border-radius:10px; padding:12px 8px; text-align:center; }}
.tile-val {{ font-size:16px; font-weight:700; }}
.tile-label {{ font-size:8.5px; color:{DIM}; margin-top:4px; text-transform:uppercase; letter-spacing:.4px; }}

.kw-row {{ display:flex; align-items:center; gap:10px; padding:5px 0; page-break-inside: avoid; }}
.kw-term {{ width:118px; font-size:11px; color:{TEXT}; }}
.kw-bar {{ flex:1; height:9px; background:{SURFACE2}; border-radius:5px; overflow:hidden; }}
.kw-fill {{ height:100%; background:linear-gradient(90deg,{ACCENT},#a78bfa); border-radius:5px; }}
.kw-meta {{ width:82px; text-align:right; font-size:10px; color:{DIM}; }}

.rec {{ display:flex; align-items:flex-start; gap:10px; padding:9px 12px; margin-bottom:7px;
  background:{SURFACE}; border:1px solid {BORDER}; border-radius:8px; page-break-inside: avoid; }}
.pill {{ font-size:9px; font-weight:700; padding:2px 8px; border-radius:20px; border:1px solid;
  letter-spacing:.5px; flex:0 0 auto; margin-top:1px; white-space:nowrap; }}
.rec-text {{ font-size:11px; color:{TEXT}; }}

.fix {{ display:flex; align-items:flex-start; gap:11px; padding:11px 13px; margin-bottom:9px;
  background:{SURFACE}; border:1px solid {BORDER}; border-radius:9px; page-break-inside: avoid; }}
.chk {{ width:18px; height:18px; border:2px solid {DIM}; border-radius:5px; flex:0 0 auto;
  margin-top:1px; display:flex; align-items:center; justify-content:center; font-size:12px; color:{ACCENT}; }}
.fix-body {{ flex:1; min-width:0; }}
.fix-head {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
.fix-issue {{ font-size:11.5px; font-weight:700; color:{TEXT}; }}
.fix-effort {{ font-size:9px; color:{DIM}; margin-left:auto; }}
.fix-text {{ font-size:10.5px; color:{TEXT}; margin-top:5px; }}
.fix-impact {{ font-size:10px; color:{DIM}; margin-top:4px; }}

.anchors {{ display:flex; flex-wrap:wrap; gap:6px; }}
.anchor {{ display:flex; align-items:center; gap:6px; background:{SURFACE2}; border:1px solid {BORDER};
  border-radius:20px; padding:3px 10px; font-size:10px; }}
.anchor-text {{ color:{TEXT}; }}
.anchor-count {{ color:{DIM}; font-weight:700; }}

.proj {{ display:flex; align-items:center; gap:14px; background:{SURFACE}; border:1px solid {BORDER};
  border-radius:10px; padding:12px 16px; margin:14px 0 4px; page-break-inside: avoid; }}
.proj-num {{ font-size:26px; font-weight:800; color:{YELLOW}; }}
.proj-label {{ font-size:10px; color:{DIM}; text-transform:uppercase; letter-spacing:.4px; }}
.proj-note {{ font-size:10px; color:{DIM}; margin-left:auto; max-width:320px; }}

.footer {{ margin-top:22px; padding-top:12px; border-top:1px solid {BORDER};
  font-size:9px; color:{DIM}; display:flex; justify-content:space-between; page-break-inside: avoid; }}
.muted {{ color:{DIM}; }}
.subhead {{ font-size:10px; color:{DIM}; margin:8px 0 10px; }}
</style></head>
<body>
  <div class="header">
    <div class="brand">
      <div class="brand-mark">H</div>
      <div>
        <div class="brand-name">MoiraiCore · SEO Toolkit</div>
        <div class="brand-sub">Intelligence Menu → SEO Audit</div>
      </div>
    </div>
    <div class="tag">
      <div>Audited <b>{esc(audited[:19].replace("T"," "))}</b></div>
      <div>Engine time <b>{audit_time}s</b></div>
    </div>
  </div>

  <div class="score-grid">
    {ring}
    <div class="score-meta">
      <div class="url">{esc(url)}</div>
      <div class="title">{esc(title)}</div>
      <div class="counts">
        {counts.get('total',0)} recommendations ·
        <span style="color:{RED}">{counts.get('high',0)} high</span> ·
        <span style="color:{YELLOW}">{counts.get('medium',0)} medium</span> ·
        <span style="color:{ACCENT}">{counts.get('low',0)} low</span>
      </div>
    </div>
  </div>

  <h2>SEO Health Signals</h2>
  <div class="tiles">{tiles}</div>

  <h2>Top Keywords (by density)</h2>
  {kw_rows or '<div class="muted">No keyword data extracted.</div>'}

  <h2>Recommendations</h2>
  {rec_rows or '<div class="muted">No recommendations — page is in good shape.</div>'}

  <h2>Link &amp; Anchor Profile</h2>
  <div class="tiles" style="grid-template-columns:repeat(4,1fr)">
    {tile("Total Links", back.get("total_links",0))}
    {tile("Internal", back.get("internal_links",0))}
    {tile("External", back.get("external_links",0))}
    {tile("Empty Anchors", back.get("empty_anchors",0), back.get("empty_anchors",0)==0, back.get("empty_anchors",0)>0)}
  </div>
  <div class="subhead">Anchor texts</div>
  <div class="anchors">{anchor_rows}</div>

  <div class="page-break"></div>

  <h2>Recommended Fixes — Lift D → B</h2>
  <div class="subhead">Prioritised, copy-paste-ready actions derived from this audit. Applying all items is projected to move the score into the B band.</div>
  <div class="proj">
    <div>
      <div class="proj-num">{proj_label}</div>
      <div class="proj-label">Projected score</div>
    </div>
    <div class="proj-note">Conservative estimate assuming the content-depth, meta, title, schema, and social fixes land. Real gain also depends on content quality and competition.</div>
  </div>
  {fix_rows}

  <div class="footer">
    <span>Generated by MoiraiCore SEO Toolkit · local-first analysis, zero API cost</span>
    <span>crownhotelparramatta.com.au</span>
  </div>
</body></html>"""


def main():
    if len(sys.argv) != 3:
        print("Usage: make_seo_pdf.py <audit.json> <output.pdf>")
        sys.exit(1)
    audit_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    data = json.loads(audit_path.read_text())
    html = render_html(data)
    Path("/tmp/seo_report.html").write_text(html)  # for visual review
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        page.pdf(path=str(out_path), format="A4", print_background=True, margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
        browser.close()
    print(f"PDF written: {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
