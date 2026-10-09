"""Local, escaped HTML and Markdown verdict pages from saved registry evidence."""

import html
from pathlib import Path
from typing import Any

from xasset.research.report import markdown
from xasset.research.suite import saved_suite
from xasset.research.trials import Registry
from xasset.store.writer import atomic_path, write_json

STYLE = """
body{font:16px/1.6 system-ui,sans-serif;background:#101824;color:#e5edf8;margin:0}
main{max-width:1000px;margin:48px auto;padding:0 24px}h1{font-size:36px;line-height:1.2}
a{color:#83c8ff}section{border:1px solid #344356;border-radius:12px;padding:24px;margin:18px 0}
.label{color:#f3ca83;font-size:13px;text-transform:uppercase;letter-spacing:.1em}
.muted{color:#aebcd0}table{border-collapse:collapse;width:100%;font-size:14px}
td,th{padding:10px;text-align:left;border-bottom:1px solid #344356}pre{white-space:pre-wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px}
.grid section{margin:0}code{overflow-wrap:anywhere}li{margin:6px 0}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{STYLE}</style><main>{body}</main></html>"
    )


def publish(registry: Registry, suite_id: str, output: Path) -> dict[str, Any]:
    suite = saved_suite(registry, suite_id)
    cards = []
    index = [f"# {suite.title}", "", "Research status only. No strategy is accepted.", ""]
    summaries = []
    inputs: dict[str, dict[str, Any]] = {}
    for case in suite.cases:
        attempts = registry.list_runs(case.experiment.family)
        latest = attempts[-1] if attempts else None
        result = (latest or {}).get("result") or {}
        gate = result.get("gate", {})
        status = gate.get("status", "not_evaluated")
        execution = latest["status"] if latest else "not_run"
        blockers = result.get("readiness", {}).get("blockers", [])
        summary = {
            "case": case.id,
            "title": case.title,
            "family": case.experiment.family,
            "execution": execution,
            "verdict": status,
            "accepted": False,
            "run_id": latest["run_id"] if latest else None,
            "attempts": len(attempts),
            "registered_candidates": len(case.experiment.parameters()),
            "blockers": blockers,
            "gate": gate,
            "original_scope": case.original_scope,
            "implemented_scope": case.implemented_scope,
            "deferred_scope": case.deferred_scope,
        }
        summaries.append(summary)
        for symbol, source in case.experiment.required_sources.items():
            key = f"{source}:{symbol}:{case.experiment.start}:{case.experiment.discovery_end}"
            inputs.setdefault(
                key,
                {
                    "symbol": symbol,
                    "source": source,
                    "start": case.experiment.start.isoformat(),
                    "end": case.experiment.discovery_end.isoformat(),
                    "cases": [],
                },
            )
            inputs[key]["cases"].append(case.id)
        esc = html.escape
        body = (
            f"<p><a href='index.html'>← All experiments</a></p>"
            f"<p class='label'>{esc(execution)} · {esc(status)}</p><h1>{esc(case.title)}</h1>"
            f"<p>{esc(case.experiment.hypothesis)}</p><section><h2>Scope</h2>"
            f"<p>{esc(case.implemented_scope)}</p><p class='muted'>Original plan: "
            f"{esc(case.original_scope)}</p><ul>"
            + "".join(f"<li>Deferred: {esc(item)}</li>" for item in case.deferred_scope)
            + "</ul></section>"
        )
        if blockers:
            body += (
                "<section><h2>Inputs needed</h2><ul>"
                + "".join(f"<li>{esc(reason)}</li>" for reason in blockers)
                + "</ul></section>"
            )
        if latest and latest["error"] and not blockers:
            body += f"<section><h2>Execution error</h2><pre>{esc(latest['error'])}</pre></section>"
        if "engine_result" in result:
            body += (
                "<section><h2>Out-of-sample results</h2><table><thead><tr>"
                "<th>Costs</th><th>Net PnL</th><th>Trades</th><th>Daily t</th>"
                "</tr></thead><tbody>"
            )
            for level, checks in gate.get("scenarios", {}).items():
                metrics = checks.get("metrics", {})
                body += (
                    "<tr>"
                    + "".join(
                        f"<td>{esc(str(value))}</td>"
                        for value in (
                            level + "x",
                            metrics.get("net_pnl"),
                            metrics.get("trade_count"),
                            metrics.get("daily_t"),
                        )
                    )
                    + "</tr>"
                )
            body += "</tbody></table></section>"
        body += (
            f"<section><h2>Evidence</h2><p>Family: <code>{esc(case.experiment.family)}</code>"
            f" · {len(case.experiment.parameters())} registered candidates"
            f" · {len(attempts)} attempts</p>"
            f"<p>Run: <code>{esc(str(summary['run_id'] or 'Not run'))}</code></p>"
            "<p>Holdout release, independent audit and reconciliation remain required. "
            "No strategy is accepted.</p>"
            f"<p><a href='{esc(case.id)}.md'>Full Markdown report</a></p></section>"
        )
        document = (
            f"# {case.title}\n\nOriginal scope: {case.original_scope}\n\n"
            f"Implemented scope: {case.implemented_scope}\n\n"
            + "".join(f"- Deferred: {item}\n" for item in case.deferred_scope)
            + "\n"
            + (markdown(latest) if latest else "No attempt recorded.\n")
            + "\nNo strategy is accepted. Blocked means not evaluated, not rejected.\n"
        )
        for suffix, content in (("html", _page(case.title, body)), ("md", document)):
            with atomic_path(output / f"{case.id}.{suffix}") as temporary:
                temporary.write_text(content)
        cards.append(
            f"<section><p class='label'>{esc(execution)} · {esc(status)}</p>"
            f"<h2><a href='{esc(case.id)}.html'>{esc(case.title)}</a></h2>"
            f"<p>{esc(case.implemented_scope)}</p>"
            f"<p class='muted'>{len(blockers)} input blockers · "
            f"{len(case.experiment.parameters())} registered candidates</p></section>"
        )
        index.append(f"- [{case.title}]({case.id}.md): {execution} / {status}")
    body = (
        f"<p class='label'>Phase 3 · Experiment registry</p><h1>{html.escape(suite.title)}</h1>"
        "<p class='muted'>Each design is registered before reading discovery data. "
        "Blocked experiments have no performance verdict. No strategy is accepted.</p>"
        "<div class='grid'>" + "".join(cards) + "</div>"
    )
    for name, content in (
        ("index.html", _page(suite.title, body)),
        ("index.md", "\n".join(index) + "\n"),
    ):
        with atomic_path(output / name) as temporary:
            temporary.write_text(content)
    write_json(output / "summary.json", {"suite": suite.id, "cases": summaries, "accepted": False})
    write_json(
        output / "required-inputs.json", {"discovery_only": True, "inputs": list(inputs.values())}
    )
    return {
        "suite": suite.id,
        "pages": len(suite.cases),
        "index": str(output / "index.html"),
        "summary": str(output / "summary.json"),
        "accepted": False,
    }
