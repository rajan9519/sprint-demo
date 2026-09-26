"""Render the sprint report (LLM JSON + computed context) to Markdown and self-contained HTML."""
from __future__ import annotations

import html
import re
from typing import Any, Dict, List

STATUS_LABEL = {
    "done": "Done", "in_progress": "In progress", "blocked": "Blocked", "planned": "Planned",
    "dropped": "Dropped", "exploratory": "Exploratory", "abandoned": "Abandoned", "trivial": "Trivial", "n/a": "—",
}


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _lst(v: Any) -> List[Any]:
    return v if isinstance(v, list) else []


# ------------------------------------------------------------------ markdown

def _md_status(status: str) -> str:
    return f"`{STATUS_LABEL.get(status, status)}`"


def render_markdown(report: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    L: List[str] = []
    title = ctx.get("title_override") or _s(report.get("sprint_title")) or "Sprint report"
    L.append(f"# {title}")
    L.append("")
    L.append(f"_Sprint window **{ctx['since']} → {ctx['until']}** ({ctx['tz_name']}) · "
             f"{ctx['engineer']} · generated {ctx['generated_at']}_")
    L.append("")
    L.append("## Executive summary")
    L.append("")
    L.append(_s(report.get("executive_summary")).strip())
    L.append("")
    if _lst(report.get("headline_achievements")):
        L.append("## Headline achievements")
        L.append("")
        for h in report["headline_achievements"]:
            L.append(f"- {h}")
        L.append("")
    L.append("## Sprint at a glance")
    L.append("")
    L.append("| Metric | Value |")
    L.append("|---|---|")
    for label, value, _hint in ctx["metrics"]:
        L.append(f"| {label} | {value} |")
    L.append("")
    if _s(report.get("metrics_commentary")).strip():
        L.append(f"_{_s(report.get('metrics_commentary')).strip()}_")
        L.append("")
    demo = _lst(report.get("demo_items"))
    if demo:
        L.append("## Demo items")
        L.append("")
        for i, d in enumerate(demo, 1):
            tix = ", ".join(_lst(d.get("tickets")))
            L.append(f"### {i}. {_s(d.get('title'))}  {_md_status(_s(d.get('status')))}" + (f"  — {tix}" if tix else ""))
            L.append("")
            L.append(f"**What changed:** {_s(d.get('what_changed'))}")
            L.append("")
            L.append(f"**Why it matters:** {_s(d.get('why_it_matters'))}")
            L.append("")
            if _lst(d.get("demo_steps")):
                L.append("**Demo steps:**")
                for n, st in enumerate(d["demo_steps"], 1):
                    L.append(f"{n}. {st}")
                L.append("")
            if _lst(d.get("talking_points")):
                L.append("**Talking points:**")
                for tp in d["talking_points"]:
                    L.append(f"- {tp}")
                L.append("")
            if _lst(d.get("evidence")):
                L.append("**Evidence:** " + "; ".join(_s(e) for e in d["evidence"]))
                L.append("")
    streams = _lst(report.get("work_streams"))
    if streams:
        L.append("## All work streams")
        L.append("")
        for w in streams:
            tix = ", ".join(_lst(w.get("tickets")))
            L.append(f"### {_s(w.get('name'))}  {_md_status(_s(w.get('status')))}" + (f"  — {tix}" if tix else ""))
            L.append("")
            L.append(_s(w.get("summary")))
            L.append("")
            for dline in _lst(w.get("details")):
                L.append(f"- {dline}")
            meta = []
            if _lst(w.get("branches")):
                meta.append("Branches: " + ", ".join(f"`{b}`" for b in w["branches"]))
            if _lst(w.get("prs")):
                meta.append("PRs: " + ", ".join(_s(p) for p in w["prs"]))
            if meta:
                L.append("")
                L.append("_" + " · ".join(meta) + "_")
            L.append("")
    for key, heading in (("technical_decisions", "Technical decisions"), ("learnings", "Learnings"),
                         ("risks_and_asks", "Risks & asks")):
        if _lst(report.get(key)):
            L.append(f"## {heading}")
            L.append("")
            for item in report[key]:
                L.append(f"- {item}")
            L.append("")
    if _lst(report.get("challenges")):
        L.append("## Challenges & resolutions")
        L.append("")
        L.append("| Challenge | Resolution |")
        L.append("|---|---|")
        for c in report["challenges"]:
            L.append(f"| {_s(c.get('problem')).replace('|', '/')} | {_s(c.get('resolution')).replace('|', '/')} |")
        L.append("")
    if _lst(report.get("carry_over")):
        L.append("## Carry-over to next sprint")
        L.append("")
        for c in report["carry_over"]:
            tix = ", ".join(_lst(c.get("tickets")))
            L.append(f"- **{_s(c.get('item'))}**" + (f" ({tix})" if tix else "") + f" — {_s(c.get('reason'))}")
        L.append("")
    # Appendices
    commits = ctx.get("commits") or []
    if commits:
        L.append(f"## Appendix A · Commits ({len(commits)})")
        L.append("")
        L.append("| Date | Commit | Subject | Branch / refs | Δ |")
        L.append("|---|---|---|---|---|")
        for c in commits:
            delta = "merge" if c["is_merge"] else f"+{c['insertions']}/−{c['deletions']} ({c['files_changed']} files)"
            L.append(f"| {c['date']} | `{c['short']}` | {c['subject'].replace('|', '/')} | "
                     f"{c['refs'].replace('|', '/')} | {delta} |")
        L.append("")
    sessions = ctx.get("sessions") or []
    if sessions:
        L.append(f"## Appendix B · Coding-agent sessions analysed ({len(sessions)})")
        L.append("")
        L.append("| Started | Source | Project | Session | Prompts | Active | Status |")
        L.append("|---|---|---|---|---|---|---|")
        for s in sessions:
            L.append(f"| {s['date']} {s['time']} | {s['entrypoint']} | {s['project']} | "
                     f"{s['title'].replace('|', '/')} | {s['prompts']} | {s['active']} | "
                     f"{STATUS_LABEL.get(s['status'], s['status'])} |")
        L.append("")
    daily = ctx.get("daily") or []
    if daily:
        L.append("## Appendix C · Daily activity")
        L.append("")
        L.append("| Day | Sessions | Prompts | Commits |")
        L.append("|---|---|---|---|")
        for d in daily:
            L.append(f"| {d['date']} ({d['weekday']}) | {d['sessions']} | {d['prompts']} | {d['commits']} |")
        L.append("")
    L.append("---")
    L.append(f"_Generated by sprint-report from local coding-agent transcripts · "
             f"models: {ctx['llm']['map_model']} (per session), {ctx['llm']['reduce_model']} (synthesis) · "
             f"{ctx['llm']['calls']} LLM calls_")
    return "\n".join(L).rstrip() + "\n"


# ---------------------------------------------------------------------- html

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{2,7})\b")


def _inline(text: Any, tickets: bool = True) -> str:
    """Escape, then allow a tiny inline-markdown subset: **bold**, `code`, [text](https://...)."""
    s = html.escape(_s(text), quote=False)
    s = _INLINE_CODE.sub(r"<code>\1</code>", s)
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    s = _LINK.sub(r'<a href="\2" rel="noopener noreferrer" target="_blank">\1</a>', s)
    if tickets:
        s = _TICKET.sub(r'<span class="tk">\1</span>', s)
    return s


def _badge(status: str) -> str:
    status = _s(status) or "planned"
    return f'<span class="badge b-{html.escape(status)}">{html.escape(STATUS_LABEL.get(status, status))}</span>'


def _ul(items: List[Any], cls: str = "") -> str:
    if not items:
        return ""
    c = f' class="{cls}"' if cls else ""
    return f"<ul{c}>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>"


def _ol(items: List[Any]) -> str:
    if not items:
        return ""
    return "<ol>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ol>"


FONTS_LINK = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&'
              'family=IBM+Plex+Mono:wght@400;500&display=swap">')

CSS = """
:root{--bg:#f5f7fa;--card:#ffffff;--ink:#14181f;--muted:#5c6573;--line:#dfe4ea;--accent:#1f5eff;--accent-ink:#ffffff;
--done:#15803d;--done-bg:#e3f6ea;--prog:#1d4ed8;--prog-bg:#e4ecff;--block:#b91c1c;--block-bg:#fde7e7;--plan:#5c6573;--plan-bg:#eaedf1;
--tk:#5b3cc4;--tk-bg:#eee9fb;--bar:#1f5eff;--bar2:#d97706;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e1116;--card:#161a21;--ink:#e7eaee;--muted:#98a1ad;--line:#262c35;
--accent:#6b9dff;--accent-ink:#0e1116;--done:#4ade80;--done-bg:#122b1c;--prog:#8db3ff;--prog-bg:#152341;--block:#f87171;--block-bg:#3a1414;--plan:#98a1ad;--plan-bg:#232932;
--tk:#bdaaff;--tk-bg:#251f45;--bar:#6b9dff;--bar2:#f2a33a;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0e1116;--card:#161a21;--ink:#e7eaee;--muted:#98a1ad;--line:#262c35;--accent:#6b9dff;--accent-ink:#0e1116;
--done:#4ade80;--done-bg:#122b1c;--prog:#8db3ff;--prog-bg:#152341;--block:#f87171;--block-bg:#3a1414;--plan:#98a1ad;--plan-bg:#232932;
--tk:#bdaaff;--tk-bg:#251f45;--bar:#6b9dff;--bar2:#f2a33a;color-scheme:dark}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
.mono,code,.tk,td.mono,.card .v,.sv,.dl{font-family:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
.wrap{max-width:960px;margin:0 auto;padding:40px 28px 72px}
header.hero{padding:24px 0 20px;border-bottom:2px solid var(--ink);margin-bottom:28px}
.kicker{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:600}
h1{font-size:32px;line-height:1.15;margin:10px 0 12px;font-weight:600;letter-spacing:-.015em;text-wrap:balance}
.sub{color:var(--muted);font-size:14px}
h2{font-size:20px;margin:44px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line);font-weight:600;letter-spacing:-.005em;text-wrap:balance}
h3{font-size:17px;margin:20px 0 8px;font-weight:600;text-wrap:balance}
p{margin:0 0 12px;max-width:75ch}
.lead{font-size:16.5px;line-height:1.62}
ul,ol{margin:6px 0 12px;padding-left:22px}li{margin:5px 0;max-width:75ch}
.lbl{font-size:11.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:14px 0 6px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:8px 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.card .v{font-size:26px;font-weight:500;line-height:1.1;letter-spacing:-.01em}
.card .vs{font-size:12.5px;font-weight:400;color:var(--muted);letter-spacing:0}
.card .l{font-size:12px;color:var(--muted);margin-top:6px}
.kv{border-top:1px solid var(--line)}
.kv table{width:100%;border-collapse:collapse;font-size:13.5px}
.kv th{width:38%;text-align:left;font-weight:600;font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);padding:9px 0;border-bottom:1px solid var(--line);vertical-align:top}
.kv td{padding:9px 0 9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
.note{color:var(--muted);font-style:italic;margin-top:12px}
.strips{margin-top:8px}
.strip{display:grid;grid-template-columns:76px 1fr;gap:12px;align-items:end}
.sl{font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);font-weight:600;padding-bottom:6px}
.sbars{display:flex;gap:4px;align-items:flex-end;height:70px;border-bottom:1px solid var(--line)}
.sb{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;min-width:0}
.sb i{display:block;width:62%;max-width:26px;background:var(--bar);border-radius:2px 2px 0 0}
.strip.c .sb i{background:var(--bar2)}
.sv{font-size:11px;color:var(--muted);margin-bottom:3px;line-height:1}
.strip.axis .sbars{height:auto;border:0;align-items:flex-start;padding-top:6px}
.dl{font-size:10.5px;color:var(--muted);text-align:center;line-height:1.25;white-space:nowrap}.dl b{display:block;color:var(--ink);font-weight:500}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px 22px;margin:14px 0}
.item h3{margin:0 0 12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:18px}
.item .num{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:50%;background:var(--accent);color:var(--accent-ink);font-size:12.5px;font-weight:600;flex:none}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media (max-width:760px){.grid2{grid-template-columns:1fr}}
.badge{display:inline-block;font-size:11.5px;font-weight:600;padding:2px 9px;border-radius:999px;letter-spacing:.02em;vertical-align:middle}
.b-done{background:var(--done-bg);color:var(--done)}.b-in_progress{background:var(--prog-bg);color:var(--prog)}
.b-blocked,.b-abandoned{background:var(--block-bg);color:var(--block)}.b-planned,.b-dropped,.b-trivial,.b-exploratory,.b-n\\/a{background:var(--plan-bg);color:var(--plan)}
.tk{display:inline-block;font-size:12px;background:var(--tk-bg);color:var(--tk);padding:0 6px;border-radius:4px}
code{font-size:12.5px;background:var(--plan-bg);padding:1px 5px;border-radius:4px}
.ev{color:var(--muted);font-size:13px;border-top:1px dashed var(--line);margin-top:14px;padding-top:10px}
.streams{border-top:1px solid var(--line)}
.stream{padding:18px 0;border-bottom:1px solid var(--line)}
.stream h3{margin:0 0 6px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.meta{color:var(--muted);font-size:13px;margin-top:6px}
.tblwrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);font-weight:600;background:var(--bg)}
tr:last-child td{border-bottom:0}td.mono{font-size:12.5px;white-space:nowrap}
.legend{font-size:12px;color:var(--muted);margin-top:8px;display:flex;gap:16px}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:-1px;margin-right:5px}
a{color:var(--accent)}a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
footer{margin-top:48px;color:var(--muted);font-size:12.5px;border-top:1px solid var(--line);padding-top:14px}
@media print{body{background:#fff}.wrap{padding:0}.item,.card,.tblwrap{break-inside:avoid;border-color:#ccc}h2{break-after:avoid}}
"""


def render_html(report: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    title = ctx.get("title_override") or _s(report.get("sprint_title")) or "Sprint report"
    page_name = ctx.get("short_title") or f"Sprint {ctx['since']} → {ctx['until']}"
    P: List[str] = []
    P.append(f"<title>{html.escape(page_name)}</title>")
    P.append(f"<style>{CSS}</style>")
    P.append(FONTS_LINK)
    P.append('<div class="wrap">')
    P.append('<header class="hero">')
    P.append('<div class="kicker">Sprint demo report</div>')
    P.append(f"<h1>{html.escape(title)}</h1>")
    P.append(f'<div class="sub">{html.escape(ctx["since"])} → {html.escape(ctx["until"])} '
             f'({html.escape(ctx["tz_name"])}) · {html.escape(ctx["engineer"])} · generated {html.escape(ctx["generated_at"])}</div>')
    P.append("</header>")

    P.append("<h2>Executive summary</h2>")
    P.append(f'<p class="lead">{_inline(report.get("executive_summary"))}</p>')
    if _lst(report.get("headline_achievements")):
        P.append('<div class="lbl">Headline achievements</div>')
        P.append(_ul(report["headline_achievements"]))

    P.append("<h2>Sprint at a glance</h2>")
    P.append('<div class="cards">')
    for label, value, hint in ctx["metrics"]:
        if hint == "card":
            # "19 +12 merges" -> big "19" with a small "+12 merges" suffix
            big, _, small = _s(value).partition(" ")
            suffix = f' <span class="vs">{html.escape(small)}</span>' if small else ""
            P.append(f'<div class="card"><div class="v">{html.escape(big)}{suffix}</div><div class="l">{html.escape(label)}</div></div>')
    P.append("</div>")
    rest = [(l, v) for l, v, h in ctx["metrics"] if h != "card"]
    if rest:
        P.append('<div class="kv"><table><tbody>')
        for l, v in rest:
            P.append(f"<tr><th>{html.escape(l)}</th><td>{_inline(v)}</td></tr>")
        P.append("</tbody></table></div>")
    if _s(report.get("metrics_commentary")).strip():
        P.append(f'<p class="note">{_inline(report.get("metrics_commentary"))}</p>')

    daily = ctx.get("daily") or []
    if daily:
        P.append('<div class="lbl" style="margin-top:22px">Daily activity</div>')
        P.append('<div class="strips">')
        for key, cls, label in (("prompts", "", "Prompts"), ("commits", "c", "Commits")):
            mx = max([d[key] for d in daily] + [1])
            P.append(f'<div class="strip {cls}"><div class="sl">{label}</div><div class="sbars">')
            for d in daily:
                v = d[key]
                h = max(2, int(50 * v / mx)) if v else 0
                val = f'<span class="sv">{v}</span>' if v else ""
                P.append(f'<div class="sb" title="{d["date"]}: {v} {label.lower()}">{val}<i style="height:{h}px"></i></div>')
            P.append("</div></div>")
        P.append('<div class="strip axis"><div class="sl"></div><div class="sbars">')
        for d in daily:
            P.append(f'<div class="sb"><span class="dl"><b>{d["weekday"]}</b>{d["date"][5:]}</span></div>')
        P.append("</div></div></div>")

    demo = _lst(report.get("demo_items"))
    if demo:
        P.append("<h2>Demo items</h2>")
        for i, d in enumerate(demo, 1):
            P.append('<article class="item">')
            tix = "".join(f' <span class="tk">{html.escape(_s(t))}</span>' for t in _lst(d.get("tickets")))
            P.append(f'<h3><span class="num">{i}</span><span>{html.escape(_s(d.get("title")))}</span>{_badge(_s(d.get("status")))}{tix}</h3>')
            P.append('<div class="grid2">')
            P.append(f'<div><div class="lbl">What changed</div><p>{_inline(d.get("what_changed"))}</p>'
                     f'<div class="lbl">Why it matters</div><p>{_inline(d.get("why_it_matters"))}</p></div>')
            P.append(f'<div><div class="lbl">Demo steps</div>{_ol(_lst(d.get("demo_steps")))}'
                     f'<div class="lbl">Talking points</div>{_ul(_lst(d.get("talking_points")))}</div>')
            P.append("</div>")
            if _lst(d.get("evidence")):
                P.append('<div class="ev"><strong>Evidence:</strong> ' + " · ".join(_inline(e) for e in d["evidence"]) + "</div>")
            P.append("</article>")

    streams = _lst(report.get("work_streams"))
    if streams:
        P.append("<h2>All work streams</h2>")
        P.append('<div class="streams">')
        for w in streams:
            P.append('<section class="stream">')
            tix = "".join(f' <span class="tk">{html.escape(_s(t))}</span>' for t in _lst(w.get("tickets")))
            P.append(f'<h3><span>{html.escape(_s(w.get("name")))}</span>{_badge(_s(w.get("status")))}{tix}</h3>')
            P.append(f"<p>{_inline(w.get('summary'))}</p>")
            P.append(_ul(_lst(w.get("details"))))
            meta = []
            if _lst(w.get("branches")):
                meta.append("Branches: " + ", ".join(f"<code>{html.escape(_s(b))}</code>" for b in w["branches"]))
            if _lst(w.get("prs")):
                meta.append("PRs: " + ", ".join(html.escape(_s(p)) for p in w["prs"]))
            if meta:
                P.append(f'<div class="meta">{" · ".join(meta)}</div>')
            P.append("</section>")
        P.append("</div>")

    two_col = []
    if _lst(report.get("technical_decisions")):
        two_col.append(("Technical decisions", _ul(report["technical_decisions"])))
    if _lst(report.get("learnings")):
        two_col.append(("Learnings", _ul(report["learnings"])))
    if two_col:
        P.append('<div class="grid2">')
        for h, body in two_col:
            P.append(f"<div><h2>{html.escape(h)}</h2>{body}</div>")
        P.append("</div>")

    if _lst(report.get("challenges")):
        P.append("<h2>Challenges &amp; resolutions</h2>")
        P.append('<div class="tblwrap"><table><thead><tr><th style="width:45%">Challenge</th><th>Resolution</th></tr></thead><tbody>')
        for c in report["challenges"]:
            P.append(f"<tr><td>{_inline(c.get('problem'))}</td><td>{_inline(c.get('resolution'))}</td></tr>")
        P.append("</tbody></table></div>")

    if _lst(report.get("carry_over")):
        P.append("<h2>Carry-over to next sprint</h2>")
        P.append("<ul>")
        for c in report["carry_over"]:
            tix = "".join(f' <span class="tk">{html.escape(_s(t))}</span>' for t in _lst(c.get("tickets")))
            P.append(f"<li><strong>{_inline(c.get('item'), tickets=False)}</strong>{tix} — {_inline(c.get('reason'))}</li>")
        P.append("</ul>")
    if _lst(report.get("risks_and_asks")):
        P.append("<h2>Risks &amp; asks</h2>")
        P.append(_ul(report["risks_and_asks"]))

    commits = ctx.get("commits") or []
    if commits:
        P.append(f"<h2>Appendix A · Commits ({len(commits)})</h2>")
        P.append('<div class="tblwrap"><table><thead><tr><th>Date</th><th>Commit</th><th>Subject</th><th>Branch / refs</th><th>Δ</th></tr></thead><tbody>')
        for c in commits:
            delta = "merge" if c["is_merge"] else f"+{c['insertions']}/−{c['deletions']} · {c['files_changed']} files"
            P.append(f"<tr><td class='mono'>{html.escape(c['date'])}</td><td class='mono'>{html.escape(c['short'])}</td>"
                     f"<td>{_inline(c['subject'])}</td><td class='mono' style='white-space:normal'>{html.escape(c['refs'])}</td>"
                     f"<td class='mono'>{html.escape(delta)}</td></tr>")
        P.append("</tbody></table></div>")

    sessions = ctx.get("sessions") or []
    if sessions:
        P.append(f"<h2>Appendix B · Coding-agent sessions analysed ({len(sessions)})</h2>")
        P.append('<div class="tblwrap"><table><thead><tr><th>Started</th><th>Source</th><th>Project</th><th>Session</th>'
                 '<th>Prompts</th><th>Active</th><th>Status</th></tr></thead><tbody>')
        for s in sessions:
            P.append(f"<tr><td class='mono'>{html.escape(s['date'])} {html.escape(s['time'])}</td><td>{html.escape(s['entrypoint'])}</td>"
                     f"<td>{html.escape(s['project'])}</td><td>{_inline(s['title'])}</td><td class='mono'>{s['prompts']}</td>"
                     f"<td class='mono'>{html.escape(s['active'])}</td><td>{_badge(s['status'])}</td></tr>")
        P.append("</tbody></table></div>")

    llm = ctx["llm"]
    P.append("<footer>Generated by <strong>sprint-report</strong> from local coding-agent transcripts and git history · "
             f"models: {html.escape(_s(llm['map_model']))} (per session), {html.escape(_s(llm['reduce_model']))} (synthesis) · "
             f"{llm['calls']} LLM calls</footer>")
    P.append("</div>")
    return "\n".join(P)
