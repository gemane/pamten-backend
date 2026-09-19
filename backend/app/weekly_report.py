"""The weekly activity digest: what people asked for, what the scrapers did,
what the imports brought, how the graph grew.

Read from stores that already exist, plus the per-week counters
(`SearchWeek`, `UsageWeek`) and the run log's `reason`/`entity_id`, which were
added because nothing else could say "how many this week" or "first scrape or
refresh". Each report is stored (`WeeklyReport`, one per week) so the next one
can report the change since the last — the only way to get growth out of a
store that keeps totals and no history.

Aggregates only: the digest names companies (public) and counts searches; it
never names who searched.
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from app.db.arcadedb import run_sql
from app.weekly import previous_week, week_bounds, week_label

log = logging.getLogger(__name__)

#: on-demand decision reasons that mean "we had never scraped this company"
FIRST_SCRAPE_REASONS = frozenset({"absent", "never_on_demand"})
#: … and the ones that mean "we had, and did it again"
REFRESH_REASONS = frozenset({"forced", "stale", "deepen"})
#: run-log sources that are imports rather than scrapes of one company
IMPORT_SOURCES = frozenset({"gleif-update", "ch-psc-update"})
#: enrichments a refresh triggers, reported by name
SEC_ENRICHMENTS = frozenset({"sec-13f", "sec-ex21", "sec-formd"})

TOP_N = 10


def _rows(sql: str, params: dict | None = None) -> list[dict]:
    try:
        return run_sql(sql, params or {})
    except Exception as exc:  # noqa: BLE001 - a missing type (fresh DB) reads as empty
        log.warning("weekly report query failed (%s): %s", sql[:60], exc)
        return []


def _names_for(ids: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for eid in ids:
        rows = _rows("SELECT name FROM Entity WHERE id = :id LIMIT 1", {"id": eid})
        if rows and rows[0].get("name"):
            out[eid] = rows[0]["name"]
    return out


def _searches(week: str) -> dict:
    rows = _rows("SELECT query, country, searches, zero_results, selected "
                 "FROM SearchWeek WHERE week = :w", {"w": week})
    total = sum(int(r.get("searches") or 0) for r in rows)
    zero = sum(int(r.get("zero_results") or 0) for r in rows)
    selected = sum(int(r.get("selected") or 0) for r in rows)
    top = sorted(rows, key=lambda r: -int(r.get("searches") or 0))[:TOP_N]
    usage = {r["event"]: int(r.get("count") or 0)
             for r in _rows("SELECT event, count FROM UsageWeek WHERE week = :w", {"w": week})
             if r.get("event")}
    return {
        "total": total, "distinct_queries": len(rows), "zero_results": zero,
        "selected": selected,
        "top": [{"query": r.get("query"), "country": r.get("country") or None,
                 "searches": int(r.get("searches") or 0),
                 "zero_results": int(r.get("zero_results") or 0)} for r in top],
        "usage": usage,
    }


def _runs_in(week: str) -> list[dict]:
    start, end = week_bounds(week)
    return _rows("SELECT source, target, status, total, error, reason, entity_id, started_at "
                 "FROM ScrapeRun WHERE started_at >= :a AND started_at < :b",
                 {"a": start.isoformat(), "b": end.isoformat()})


def _short_error(err: str) -> str:
    """The first clause of an error, without the URL: "Server error '500
    Internal Server Error' for url 'https://efts…'" says everything a summary
    needs before the quote closes."""
    err = (err or "").strip()
    for stop in (" for url ", " for URL ", "\n"):
        if stop in err:
            err = err.split(stop, 1)[0]
    return err[:120]


def _collapse_failures(failures: list[dict]) -> list[dict]:
    """Identical failures as one line with a count — three EFTS 500s on the
    same target are one fact, not three."""
    out: dict[tuple, dict] = {}
    for f in failures:
        key = (f["source"], f["target"], f["error"])
        d = out.setdefault(key, {**f, "count": 0})
        d["count"] += 1
    return sorted(out.values(), key=lambda d: (-d["count"], d["source"], d["target"] or ""))


def _scrapes(runs: list[dict]) -> dict:
    by_source: dict[str, Counter] = defaultdict(Counter)
    first: dict[str, dict] = {}
    refreshed: dict[str, dict] = {}
    written = 0
    failures: list[dict] = []
    enrich: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        src = r.get("source") or "?"
        if src in IMPORT_SOURCES:
            continue
        by_source[src][r.get("status") or "?"] += 1
        written += int(r.get("total") or 0) if r.get("status") == "ok" else 0
        if r.get("status") == "failed":
            failures.append({"source": src, "target": r.get("target"),
                             "error": _short_error(r.get("error") or "")})
        if src in SEC_ENRICHMENTS and r.get("status") == "ok" and int(r.get("total") or 0) > 0:
            enrich[src].append({"target": r.get("target"), "total": int(r.get("total") or 0)})
        eid, reason = r.get("entity_id"), r.get("reason")
        if eid and reason in FIRST_SCRAPE_REASONS:
            first.setdefault(eid, {"entity_id": eid, "target": r.get("target"), "records": 0})
            first[eid]["records"] += int(r.get("total") or 0)
        elif eid and reason in REFRESH_REASONS:
            refreshed.setdefault(eid, {"entity_id": eid, "target": r.get("target"), "records": 0})
            refreshed[eid]["records"] += int(r.get("total") or 0)
    # A company scraped for the first time and refreshed in the same week is a
    # first scrape — that is the more interesting fact.
    for eid in list(refreshed):
        if eid in first:
            first[eid]["records"] += refreshed.pop(eid)["records"]
    names = _names_for(set(first) | set(refreshed))
    for d in (*first.values(), *refreshed.values()):
        d["name"] = names.get(d["entity_id"]) or d["target"]
    return {
        "runs": sum(sum(c.values()) for c in by_source.values()),
        "by_source": {s: dict(c) for s, c in sorted(by_source.items())},
        "records_written": written,
        "first_scrapes": sorted(first.values(), key=lambda d: -d["records"]),
        "refreshed": sorted(refreshed.values(), key=lambda d: -d["records"]),
        "failures": _collapse_failures(failures)[:50],
        "sec_enrichments": {s: sorted(v, key=lambda d: -d["total"])[:TOP_N]
                            for s, v in sorted(enrich.items())},
    }


def _imports(runs: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for r in runs:
        src = r.get("source")
        if src not in IMPORT_SOURCES:
            continue
        d = out.setdefault(src, {"runs": 0, "ok": 0, "failed": 0, "skipped": 0, "records": 0})
        d["runs"] += 1
        st = r.get("status") or ""
        if st in d:
            d[st] += 1
        d["records"] += int(r.get("total") or 0)
    return out


def _graph_totals() -> dict:
    type_map = {"Entity": "companies", "Person": "people", "OWNS": "relationships",
                "HAS_ROLE": "roles", "Claim": "claims"}
    out = dict.fromkeys(type_map.values(), 0)
    for r in _rows("SELECT name, records FROM schema:types"):
        k = type_map.get(r.get("name"))
        if k:
            out[k] = int(r.get("records") or 0)
    return out


def _new_relationships(week: str) -> dict:
    start, end = week_bounds(week)
    out: dict[str, int] = {}
    for r in _rows("SELECT kind, count(*) AS n FROM Claim "
                   "WHERE first_seen_at >= :a AND first_seen_at < :b GROUP BY kind",
                   {"a": start.isoformat(), "b": end.isoformat()}):
        if r.get("kind"):
            out[r["kind"]] = int(r.get("n") or 0)
    return out


def _previous_totals(week: str) -> dict | None:
    rows = _rows("SELECT week, json FROM WeeklyReport WHERE week < :w ORDER BY week DESC LIMIT 1",
                 {"w": week})
    if not rows:
        return None
    try:
        prev = json.loads(rows[0].get("json") or "{}")
        return {"week": rows[0].get("week"), **(prev.get("graph", {}).get("totals") or {})}
    except ValueError:
        return None


def weekly_report(week: str | None = None) -> dict:
    """The digest for one calendar week (the last completed one by default)."""
    week = week or previous_week()
    start, end = week_bounds(week)
    runs = _runs_in(week)
    totals = _graph_totals()
    prev = _previous_totals(week)
    delta = ({k: totals[k] - int(prev.get(k) or 0) for k in totals} if prev else None)
    return {
        "week": week, "label": week_label(week),
        # Inclusive on both ends — the week is Monday to Sunday, and a subtitle
        # that reads "to Monday" beside a title that says "to Sunday" is a bug.
        "from": start.date().isoformat(), "to": (end - timedelta(days=1)).date().isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "searches": _searches(week),
        "scrapes": _scrapes(runs),
        "imports": _imports(runs),
        "graph": {"totals": totals, "new_relationships": _new_relationships(week),
                  "since": prev["week"] if prev else None, "delta": delta},
    }


def store_report(report: dict) -> None:
    """Keep one digest per week — the baseline the next one measures growth from."""
    try:
        run_sql("UPDATE WeeklyReport SET week = :w, generated_at = :g, json = :j "
                "UPSERT WHERE week = :w",
                {"w": report["week"], "g": report["generated_at"], "j": json.dumps(report)})
    except Exception as exc:  # noqa: BLE001 - a digest that cannot be stored is still a digest
        log.warning("could not store weekly report %s: %s", report.get("week"), exc)


# ── rendering ────────────────────────────────────────────────────────────────

def _fmt_delta(n: int | None) -> str:
    if n is None:
        return ""
    return f" ({'+' if n >= 0 else ''}{n:,})"


def format_report_text(r: dict) -> str:
    s, sc, im, g = r["searches"], r["scrapes"], r["imports"], r["graph"]
    lines = [f"Owlgraph Report — {r['label']}", ""]
    lines += ["SEARCHES",
              f"  {s['total']:,} searches for {s['distinct_queries']:,} distinct queries; "
              f"{s['zero_results']:,} found nothing; {s['selected']:,} led to a result"]
    for t in s["top"]:
        c = f" [{t['country']}]" if t["country"] else ""
        z = f"  (no result ×{t['zero_results']})" if t["zero_results"] else ""
        lines.append(f"    {t['searches']:>4}  {t['query']}{c}{z}")
    if s["usage"]:
        lines.append("  usage: " + ", ".join(f"{k} {v}" for k, v in sorted(s["usage"].items())))
    lines += ["", "SCRAPES",
              f"  {sc['runs']:,} runs, {sc['records_written']:,} records written"]
    for src, st in sc["by_source"].items():
        lines.append(f"    {src:16} " + ", ".join(f"{k} {v}" for k, v in sorted(st.items())))
    if sc["first_scrapes"]:
        lines.append(f"  scraped for the first time ({len(sc['first_scrapes'])}):")
        lines += [f"    {d['name']}" for d in sc["first_scrapes"][:TOP_N]]
    if sc["refreshed"]:
        lines.append(f"  refreshed ({len(sc['refreshed'])}):")
        lines += [f"    {d['name']}" for d in sc["refreshed"][:TOP_N]]
    for src, items in sc["sec_enrichments"].items():
        lines.append(f"  {src}: " + "; ".join(dict.fromkeys(d['target'] for d in items)))
    if sc["failures"]:
        n_fail = sum(f.get("count", 1) for f in sc["failures"])
        lines.append(f"  failures ({n_fail}):")
        lines += [f"    {f['source']} {f['target']}: {f['error']}"
                  + (f"  ×{f['count']}" if f.get("count", 1) > 1 else "")
                  for f in sc["failures"][:TOP_N]]
    lines += ["", "IMPORTS"]
    if im:
        for src, d in im.items():
            lines.append(f"  {src}: {d['runs']} runs (ok {d['ok']}, skipped {d['skipped']}, "
                         f"failed {d['failed']}), {d['records']:,} records")
    else:
        lines.append("  none")
    d = g["delta"] or {}
    lines += ["", "GRAPH" + (f" (change since {g['since']})" if g["since"] else "")]
    for k in ("companies", "people", "relationships", "roles", "claims"):
        lines.append(f"  {k:14} {g['totals'].get(k, 0):>10,}{_fmt_delta(d.get(k) if d else None)}")
    if g["new_relationships"]:
        lines.append("  first asserted this week: " +
                     ", ".join(f"{k} {v:,}" for k, v in sorted(g["new_relationships"].items())))
    lines += ["", "Automated weekly report from the Owlgraph backend for the operator's records"
              + (f", sent to {r['recipient']}" if r.get("recipient") else "") + ".",
              "Aggregates only — companies are named, people who searched are not. "
              "Internal; not for redistribution."]
    return "\n".join(lines) + "\n"


def format_report_html(r: dict) -> str:
    """The digest as an email: a headline row of the four numbers, then one
    card per section. Inline styles only (mail clients strip stylesheets), no
    images, no scripts; tables for layout because that is what renders the
    same everywhere. Company lists are names only — the counts behind them
    are on the Scraper tab for whoever wants them."""
    import html as _h
    e = _h.escape
    s, sc, im, g = r["searches"], r["scrapes"], r["imports"], r["graph"]
    d = g["delta"] or {}

    font = "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
    muted = "color:#6b7280;"
    card = ("background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;"
            "padding:16px 18px;margin:0 0 14px;")
    h2 = f"margin:0 0 10px;font-size:13px;letter-spacing:.06em;text-transform:uppercase;{muted}"
    li = "padding:3px 0;border-bottom:1px solid #f3f4f6;"

    def stat(n, label):
        return (f"<td style='padding:0 18px 0 0;vertical-align:top'>"
                f"<div style='font-size:26px;font-weight:700;color:#111827;line-height:1.1'>{n:,}</div>"
                f"<div style='font-size:12px;{muted}'>{e(label)}</div></td>")

    def names(items, cap=TOP_N):
        rows = "".join(f"<li style='{li}'>{e(x['name'])}</li>" for x in items[:cap])
        more = f"<li style='{li}{muted}'>… and {len(items) - cap} more</li>" if len(items) > cap else ""
        return f"<ul style='list-style:none;margin:6px 0 0;padding:0;font-size:14px;color:#111827'>{rows}{more}</ul>"

    def delta(k):
        if not d or k not in d:
            return ""
        n = d[k]
        col = "#15803d" if n > 0 else ("#b91c1c" if n < 0 else "#6b7280")
        return f" <span style='color:{col};font-size:12px'>({'+' if n >= 0 else ''}{n:,})</span>"

    out = [f"<html><body style='margin:0;padding:24px;background:#f3f4f6;{font}'>",
           "<div style='max-width:640px;margin:0 auto'>",
           f"<h1 style='margin:0 0 4px;font-size:20px;color:#111827'>Owlgraph Report — {e(r['label'])}</h1>",
           f"<div style='font-size:12px;{muted}margin:0 0 18px'>{e(r['from'])} to {e(r['to'])}</div>",
           # headline numbers
           f"<div style='{card}'><table role='presentation' cellpadding='0' cellspacing='0'><tr>",
           stat(s["total"], "searches"), stat(sc["runs"], "scrape runs"),
           stat(len(sc["first_scrapes"]), "new companies"), stat(len(sc["refreshed"]), "refreshed"),
           "</tr></table></div>"]

    # searches
    out.append(f"<div style='{card}'><h2 style='{h2}'>Searches</h2>")
    out.append(f"<div style='font-size:13px;{muted}'>{s['distinct_queries']:,} distinct queries · "
               f"{s['zero_results']:,} found nothing · {s['selected']:,} led to a result</div>")
    if s["top"]:
        rows = ""
        for t in s["top"]:
            tag = (f" <span style='font-size:11px;{muted}'>[{e(t['country'])}]</span>" if t["country"] else "")
            zero = (" <span style='font-size:11px;color:#b45309'>no result</span>" if t["zero_results"] else "")
            rows += (f"<tr><td style='padding:3px 10px 3px 0;text-align:right;font-weight:600;color:#111827'>{t['searches']}</td>"
                     f"<td style='padding:3px 0;color:#111827'>{e(t['query'])}{tag}{zero}</td></tr>")
        out.append(f"<table role='presentation' cellpadding='0' cellspacing='0' style='font-size:14px;margin-top:8px'>{rows}</table>")
    out.append("</div>")

    # scrapes — tables, not prose: runs by source, the two company lists side
    # by side, the SEC enrichments, then the failures.
    th = f"padding:4px 12px 4px 0;text-align:left;font-size:11px;letter-spacing:.04em;text-transform:uppercase;{muted}border-bottom:1px solid #e5e7eb;"
    thn = th.replace("text-align:left", "text-align:right")
    td = "padding:4px 12px 4px 0;color:#111827;border-bottom:1px solid #f3f4f6;"
    tdn = td + "text-align:right;font-variant-numeric:tabular-nums;"
    out.append(f"<div style='{card}'><h2 style='{h2}'>Scrapes</h2>")
    if sc["by_source"]:
        cols = ["ok", "skipped", "failed", "running"]
        rows = ""
        for src, st in sc["by_source"].items():
            cells = "".join(f"<td style='{tdn}'>{st.get(c, 0) or '·'}</td>" for c in cols)
            rows += f"<tr><td style='{td}'><b>{e(src)}</b></td>{cells}</tr>"
        tot = {c: sum(st.get(c, 0) for st in sc["by_source"].values()) for c in cols}
        rows += ("<tr>" + f"<td style='{td}border-bottom:0;{muted}'>total</td>"
                 + "".join(f"<td style='{tdn}border-bottom:0;font-weight:600'>{tot[c] or '·'}</td>" for c in cols)
                 + "</tr>")
        out.append("<table role='presentation' cellpadding='0' cellspacing='0' style='font-size:13px;width:100%'>"
                   f"<tr><th style='{th}'>source</th>" + "".join(f"<th style='{thn}'>{c}</th>" for c in cols)
                   + f"</tr>{rows}</table>")
    else:
        out.append(f"<div style='font-size:13px;{muted}'>no scrape runs</div>")

    if sc["first_scrapes"] or sc["refreshed"]:
        cap = f"font-size:11px;letter-spacing:.04em;text-transform:uppercase;{muted}padding:0 0 4px;"
        col_style = "vertical-align:top;width:50%;padding:0 12px 0 0;"
        first = names(sc["first_scrapes"]) if sc["first_scrapes"] else f"<div style='font-size:13px;{muted}'>none</div>"
        refr = names(sc["refreshed"]) if sc["refreshed"] else f"<div style='font-size:13px;{muted}'>none</div>"
        out.append("<table role='presentation' cellpadding='0' cellspacing='0' style='width:100%;margin-top:14px'><tr>"
                   f"<td style='{col_style}'><div style='{cap}'>Scraped for the first time ({len(sc['first_scrapes'])})</div>{first}</td>"
                   f"<td style='{col_style}padding-right:0'><div style='{cap}'>Refreshed ({len(sc['refreshed'])})</div>{refr}</td>"
                   "</tr></table>")

    if sc["sec_enrichments"]:
        rows = ""
        for src, items in sc["sec_enrichments"].items():
            targets = list(dict.fromkeys(x["target"] for x in items))
            rows += (f"<tr><td style='{td}white-space:nowrap;vertical-align:top'><b>{e(src)}</b></td>"
                     f"<td style='{td}'>{e(', '.join(targets))}</td></tr>")
        out.append("<table role='presentation' cellpadding='0' cellspacing='0' style='font-size:13px;width:100%;margin-top:14px'>"
                   f"<tr><th style='{th}white-space:nowrap'>SEC enrichment</th><th style='{th}'>companies</th></tr>{rows}</table>")

    if sc["failures"]:
        n_fail = sum(f.get("count", 1) for f in sc["failures"])
        rows = ""
        for f in sc["failures"][:TOP_N]:
            cnt = f"<span style='font-weight:700;color:#b91c1c'>×{f['count']}</span>" if f.get("count", 1) > 1 else "·"
            rows += (f"<tr><td style='{td}white-space:nowrap'><b>{e(f['source'])}</b></td>"
                     f"<td style='{td}'>{e(f['target'] or '')}</td>"
                     f"<td style='{tdn}'>{cnt}</td>"
                     f"<td style='{td}{muted}'>{e(f['error'])}</td></tr>")
        out.append("<table role='presentation' cellpadding='0' cellspacing='0' style='font-size:13px;width:100%;margin-top:14px'>"
                   f"<tr><th style='{th}color:#b91c1c'>failures ({n_fail})</th><th style='{th}'>target</th>"
                   f"<th style='{thn}'>×</th><th style='{th}'>error</th></tr>{rows}</table>")
    out.append("</div>")

    # imports
    out.append(f"<div style='{card}'><h2 style='{h2}'>Imports</h2>")
    if im:
        out.append("<ul style='list-style:none;margin:0;padding:0;font-size:14px;color:#111827'>" +
                   "".join(f"<li style='{li}'><b>{e(src)}</b> <span style='{muted}'>"
                           f"{v['runs']} runs · ok {v['ok']} · skipped {v['skipped']} · failed {v['failed']}"
                           f"</span></li>" for src, v in im.items()) + "</ul>")
    else:
        out.append(f"<div style='font-size:13px;{muted}'>none</div>")
    out.append("</div>")

    # graph
    since = f" <span style='font-weight:400;text-transform:none;letter-spacing:0'>· change since {e(g['since'])}</span>" if g["since"] else ""
    out.append(f"<div style='{card}'><h2 style='{h2}'>Graph{since}</h2>"
               "<table role='presentation' cellpadding='0' cellspacing='0' style='font-size:14px'>")
    for k in ("companies", "people", "relationships", "roles"):
        out.append(f"<tr><td style='padding:3px 24px 3px 0;color:#111827'>{k}</td>"
                   f"<td style='padding:3px 0;text-align:right;font-weight:600;color:#111827'>"
                   f"{g['totals'].get(k, 0):,}{delta(k)}</td></tr>")
    out.append("</table>")
    if g["new_relationships"]:
        out.append(f"<div style='font-size:12px;{muted}margin-top:8px'>first asserted this week: " +
                   ", ".join(f"{e(k)} {v:,}" for k, v in sorted(g["new_relationships"].items())) + "</div>")
    out.append("</div>")
    out.append(f"<div style='font-size:11px;{muted}line-height:1.5'>"
               "Automated weekly report from the Owlgraph backend for the operator's records"
               + (f", sent to {e(r['recipient'])}" if r.get("recipient") else "") + ". "
               "Aggregates only — companies are named, people who searched are not. "
               "Internal; not for redistribution. The same numbers are on the Scraper tab.</div>")
    out.append("</div></body></html>")
    return "".join(out)
