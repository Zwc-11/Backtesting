"""Resume Alpaca discovery-only downloads from immutable registered designs.

Run from the checkout with: uv run --frozen python scripts/backfill_equities.py plan
The run command requires runtime credential bindings and starts with a small
data probe. This operational script does not change the registered engine code.
"""

import argparse
import csv
import hashlib
import html
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from xasset.config import Instrument
from xasset.ingest.history import ingest
from xasset.normalize.calendars import calendar, expected_bar_ends
from xasset.qc.checks import check_bars
from xasset.research.data import snapshot
from xasset.research.experiment import canonical_json, digest
from xasset.research.readiness import inspect
from xasset.research.suite import saved_suite
from xasset.research.trials import Registry
from xasset.store.writer import atomic_path, write_json, writer_lock

CREDENTIALS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")


def probe_for(job: dict[str, Any]) -> dict[str, Any]:
    item = Instrument.model_validate(job["instrument"])
    start, end = datetime.fromisoformat(job["start"]), datetime.fromisoformat(job["end"])
    cal = calendar(item.calendar)
    for session in cal.sessions_in_range(start.date(), end.date()):
        opened = max(start, cal.session_open(session).to_pydatetime())
        closed = min(
            end, opened + timedelta(minutes=30), cal.session_close(session).to_pydatetime()
        )
        if opened < closed:
            return {
                "instrument": item.model_dump(mode="json"),
                "start": opened.isoformat(),
                "end": closed.isoformat(),
            }
    raise ValueError("No trading session exists inside the registered discovery interval")


def plan(registry: Registry, suite_id: str, symbols: list[str] | None = None) -> dict[str, Any]:
    suite = saved_suite(registry, suite_id)
    definitions: dict[str, dict[str, Any]] = {}
    bounds = {(case.experiment.start, case.experiment.discovery_end) for case in suite.cases}
    if len(bounds) != 1:
        raise ValueError("A shared backfill requires identical discovery bounds across the suite")
    start, end = next(iter(bounds))
    for case in suite.cases:
        saved = registry.family(case.experiment.family)
        if saved["vault_run"] is not None:
            raise ValueError("Discovery backfill is closed for a family whose vault was released")
        for item in saved["instruments"]:
            if case.experiment.required_sources.get(item["id"]) != "alpaca":
                continue
            if item["id"] in definitions and definitions[item["id"]] != item:
                raise ValueError("Conflicting registered instrument metadata across families")
            definitions[item["id"]] = item
    chosen = set(symbols) if symbols else set(definitions)
    if not chosen or chosen - definitions.keys():
        raise ValueError("Choose nonempty Alpaca symbols from the registered suite")
    # The four crypto-equity followers can unlock the BTC experiment first.
    priority = {name: index for index, name in enumerate(("COIN", "MARA", "MSTR", "RIOT", "SPY"))}
    ordered = sorted(chosen, key=lambda name: (priority.get(name, 99), name))
    jobs = []
    for name in ordered:
        item = Instrument.model_validate(definitions[name])
        if item.session != "regular" or not item.calendar:
            raise ValueError("This backfill requires registered regular-session equities")
        cursor = start
        while cursor < end:
            month_end = (cursor.replace(day=28) + timedelta(days=4)).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            stop = min(month_end, end)
            job = {
                "instrument": definitions[name],
                "start": cursor.isoformat(),
                "end": stop.isoformat(),
            }
            jobs.append({"id": digest(job), **job})
            cursor = stop
    return {
        "schema_version": 1,
        "suite": suite_id,
        "discovery_only": True,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "symbols": ordered,
        "probe": probe_for(jobs[0]),
        "jobs": jobs,
    }


def fingerprint(root: Path, job: dict[str, Any]) -> tuple[str, int]:
    item = Instrument.model_validate(job["instrument"])
    with writer_lock(root):
        bars, checksum = snapshot(
            root,
            [item],
            datetime.fromisoformat(job["start"]),
            datetime.fromisoformat(job["end"]),
            {item.id: "alpaca"},
        )
    feeds = {
        flag for flags in bars["flags"].to_list() for flag in flags if flag.startswith("feed_")
    }
    if feeds != {f"feed_{item.alpaca_feed}"}:
        raise ValueError("Downloaded feed differs from the registered feed")
    return checksum, bars.height


def download(root: Path, job: dict[str, Any], client: httpx.Client) -> dict[str, Any]:
    item = Instrument.model_validate(job["instrument"])
    report = ingest(
        root,
        [item],
        "alpaca",
        datetime.fromisoformat(job["start"]),
        datetime.fromisoformat(job["end"]),
        client=client,
    )
    result: dict[str, Any] = {"status": report["status"], "ingest_run": report["run_id"]}
    if report["status"] == "ok":
        checksum, rows = fingerprint(root, job)
        result.update(data_sha256=checksum, rows=rows)
    else:
        result["errors"] = [entry.get("error", entry["status"]) for entry in report["instruments"]]
    return result


def run(registry: Registry, design: dict[str, Any], client: httpx.Client) -> dict[str, Any]:
    missing = [name for name in CREDENTIALS if not os.environ.get(name)]
    if missing:
        return {
            "status": "blocked",
            "reason": "Missing runtime credential bindings",
            "missing": missing,
            "downloaded_jobs": 0,
            "reused_jobs": 0,
        }
    root = registry.root
    folder = root / "backfills"
    checkpoint = folder / f"{design['suite']}-alpaca.json"
    completed = reused = 0
    with writer_lock(folder):
        state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {"jobs": {}}
        state.update(
            suite=design["suite"],
            schema_version=1,
            script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        )
        pending = []
        for job in design["jobs"]:
            saved = state["jobs"].get(job["id"], {})
            valid = False
            if saved.get("status") == "ok":
                try:
                    checksum, rows = fingerprint(root, job)
                    valid = checksum == saved["data_sha256"] and rows == saved["rows"]
                except (ValueError, OSError):
                    pass
            if valid:
                reused += 1
            else:
                pending.append(job)
        # Check authentication and feed access before the bulk queue. A complete
        # resumed queue makes no network call. Only historical GETs are used.
        if pending:
            # Probe the first pending chunk, never revise a completed chunk
            # after validating its saved fingerprint for reuse.
            probe = probe_for(pending[0])
            state["probe"] = {"job": probe, **download(root, probe, client)}
            write_json(checkpoint, state)
            if state["probe"]["status"] != "ok":
                return {
                    "status": "failed",
                    "stage": "probe",
                    "probe": state["probe"],
                    "downloaded_jobs": 0,
                    "reused_jobs": reused,
                }
        for job in pending:
            result = download(root, job, client)
            state["jobs"][job["id"]] = {
                **result,
                "job": job,
                "finished_at": datetime.now(UTC).isoformat(),
            }
            write_json(checkpoint, state)
            print(
                canonical_json({"symbol": job["instrument"]["id"], "end": job["end"], **result}),
                flush=True,
            )
            if result["status"] != "ok":
                return {
                    "status": "failed",
                    "stage": "history",
                    "job": job["id"],
                    "downloaded_jobs": completed,
                    "reused_jobs": reused,
                    "checkpoint": str(checkpoint),
                }
            completed += 1
    readiness = []
    for case in saved_suite(registry, design["suite"]).cases:
        family = registry.family(case.experiment.family)
        instruments = [Instrument.model_validate(item) for item in family["instruments"]]
        with writer_lock(root):
            status = inspect(root, case.experiment, instruments)
        readiness.append({"case": case.id, **status})
    return {
        "status": "downloaded",
        "downloaded_jobs": completed,
        "reused_jobs": reused,
        "checkpoint": str(checkpoint),
        "readiness": readiness,
        "note": "Download completion does not establish coverage or a research verdict",
    }


def audit(registry: Registry, design: dict[str, Any]) -> dict[str, Any]:
    """Verify discovery evidence offline; never start a trial or repair an input."""
    root = registry.root
    checkpoint = root / "backfills" / f"{design['suite']}-alpaca.json"
    attempts_before = digest(registry.list_runs())
    suite = saved_suite(registry, design["suite"])
    families = {case.id: registry.family(case.experiment.family) for case in suite.cases}
    jobs, sessions, symbols = [], [], []
    observations: dict[str, set[datetime]] = {}
    with writer_lock(root):
        state = json.loads(checkpoint.read_text())
        for job in design["jobs"]:
            item = Instrument.model_validate(job["instrument"])
            start, end = datetime.fromisoformat(job["start"]), datetime.fromisoformat(job["end"])
            saved = state["jobs"].get(job["id"], {})
            errors: list[str] = []
            entry: dict[str, Any] = {"id": job["id"], "symbol": item.id, "errors": errors}
            try:
                if saved.get("status") != "ok" or saved.get("job") != job:
                    raise ValueError("Missing or mismatched completed checkpoint")
                bars, checksum = snapshot(root, [item], start, end, {item.id: "alpaca"})
                if checksum != saved["data_sha256"] or bars.height != saved["rows"]:
                    errors.append("Normalized rows differ from the completed checkpoint")
                if item.calendar is None:
                    raise ValueError("Registered equity session calendar is required")
                expected = expected_bar_ends(item.calendar, start, end)
                observed = set(bars["ts_end"].to_list())
                if observed - expected:
                    errors.append("Normalized rows fall outside registered sessions")
                feeds = {
                    flag
                    for flags in bars["flags"].to_list()
                    for flag in flags
                    if flag.startswith("feed_")
                }
                if feeds != {f"feed_{item.alpaca_feed}"}:
                    errors.append("Normalized feed differs from registration")
                manifest = json.loads((root / "runs" / f"{saved['ingest_run']}.json").read_text())
                if any(manifest.get(key) != job[key] for key in ("start", "end")):
                    errors.append("Ingest range differs from registration")
                results = manifest["instruments"]
                if manifest["status"] != "ok" or len(results) != 1:
                    raise ValueError("Ingest manifest does not describe one successful instrument")
                result = results[0]
                if result["instrument_config"] != job["instrument"]:
                    errors.append("Ingest instrument differs from registration")
                raw_stamps: set[datetime] = set()
                artifacts = []
                pages = []
                for batch in result["batches"]:
                    if batch.get("feed") != item.alpaca_feed:
                        errors.append("Raw batch feed differs from registration")
                    for artifact in batch["artifacts"]:
                        path = (root / artifact["path"]).resolve()
                        if path.parent != (root / "raw" / "alpaca" / item.id).resolve():
                            raise ValueError("Raw artifact path is outside its source directory")
                        payload = path.read_bytes()
                        raw_hash = hashlib.sha256(payload).hexdigest()
                        if raw_hash != path.stem:
                            errors.append("Raw archive checksum mismatch")
                        page = json.loads(payload)
                        if page.get("symbol", item.alpaca_symbol) != item.alpaca_symbol:
                            errors.append("Raw response symbol differs from registration")
                        pages.append(page)
                        artifacts.append({"path": artifact["path"], "sha256": raw_hash})
                        for row in page.get("bars") or []:
                            stamp = datetime.fromisoformat(row["t"]) + timedelta(minutes=1)
                            if stamp in expected:
                                raw_stamps.add(stamp)
                if not pages or pages[-1].get("next_page_token"):
                    errors.append("Raw pagination has no terminal response")
                if any(not page.get("next_page_token") for page in pages[:-1]):
                    errors.append("Unexpected response after terminal pagination")
                if raw_stamps != observed:
                    errors.append("Normalized timestamps differ from bounded raw responses")
                observations.setdefault(item.id, set()).update(observed)
                entry.update(
                    rows=bars.height,
                    data_sha256=checksum,
                    quality=check_bars(bars).to_dict(),
                    expected_minutes=len(expected),
                    missing_minutes=len(expected - observed),
                    raw_missing_minutes=len(expected - raw_stamps),
                    artifacts=artifacts,
                    ingest_run=saved["ingest_run"],
                )
            except (ValueError, KeyError, OSError, TypeError) as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            entry["ok"] = not errors
            jobs.append(entry)
        start, end = datetime.fromisoformat(design["start"]), datetime.fromisoformat(design["end"])
        for symbol in design["symbols"]:
            item = Instrument.model_validate(
                next(
                    job["instrument"] for job in design["jobs"] if job["instrument"]["id"] == symbol
                )
            )
            observed = observations.get(symbol, set())
            if item.calendar is None:
                raise ValueError("Registered equity session calendar is required")
            expected = expected_bar_ends(item.calendar, start, end)
            thresholds = sorted(
                {
                    case.experiment.minimum_coverage
                    for case in suite.cases
                    if symbol in case.experiment.required_sources
                }
            )
            ratio = len(expected & observed) / len(expected) if expected else 0.0
            symbols.append(
                {
                    "symbol": symbol,
                    "feed": item.alpaca_feed,
                    "rows": len(observed),
                    "expected_minutes": len(expected),
                    "missing_minutes": len(expected - observed),
                    "coverage": ratio,
                    "required_coverage": thresholds,
                    "coverage_met": bool(expected) and all(ratio >= value for value in thresholds),
                    "evidence_ok": all(job["ok"] for job in jobs if job["symbol"] == symbol),
                }
            )
            cal = calendar(item.calendar)
            for label in cal.sessions_in_range(start.date(), end.date()):
                ends = {
                    minute.to_pydatetime() + timedelta(minutes=1)
                    for minute in cal.session_minutes(label)
                } & expected
                if not ends:
                    continue
                missing = ends - observed
                sessions.append(
                    {
                        "symbol": symbol,
                        "session": str(label.date()),
                        "expected": len(ends),
                        "observed": len(ends & observed),
                        "missing": len(missing),
                        "first_missing": min(missing).isoformat() if missing else None,
                        "last_missing": max(missing).isoformat() if missing else None,
                        "opening_bar_present": min(ends) in observed,
                        "closing_bar_present": max(ends) in observed,
                    }
                )
        readiness = [
            {
                "case": case.id,
                "spec_hash": families[case.id]["spec_hash"],
                **inspect(
                    root,
                    case.experiment,
                    [Instrument.model_validate(item) for item in families[case.id]["instruments"]],
                ),
            }
            for case in suite.cases
        ]
    attempts_unchanged = attempts_before == digest(registry.list_runs())
    ok = all(job["ok"] for job in jobs) and attempts_unchanged
    return {
        "schema_version": 1,
        "suite": design["suite"],
        "discovery_only": True,
        "generated_at": datetime.now(UTC).isoformat(),
        "start": design["start"],
        "end": design["end"],
        "status": "verified" if ok else "failed",
        "attempts_unchanged": attempts_unchanged,
        "jobs": jobs,
        "symbols": symbols,
        "sessions": sessions,
        "readiness": readiness,
        "note": "Coverage is not independent price validation or a performance verdict. "
        "Missing raw IEX minutes do not by themselves identify outages, halts or no-trade minutes.",
    }


def publish_audit(output: Path, result: dict[str, Any]) -> None:
    """Write a readable evidence report and one row per equity/session."""
    write_json(output, result)
    fields = [
        "symbol",
        "session",
        "expected",
        "observed",
        "missing",
        "first_missing",
        "last_missing",
        "opening_bar_present",
        "closing_bar_present",
    ]
    with atomic_path(output.with_suffix(".csv")) as temporary:
        with temporary.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(result["sessions"])
    esc = html.escape
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{esc(str(value))}</td>"
            for value in (
                row["symbol"],
                row["rows"],
                row["missing_minutes"],
                f"{row['coverage']:.2%}",
                "Met" if row["coverage_met"] else "Below requirement",
                "Verified" if row["evidence_ok"] else "Failed",
            )
        )
        + "</tr>"
        for row in result["symbols"]
    )
    cases = "".join(
        f"<h3>{esc(case['case'])}</h3><ul>"
        + "".join(f"<li>{esc(reason)}</li>" for reason in case["blockers"])
        + ("<li>Inputs ready for registered discovery execution.</li>" if case["ready"] else "")
        + "</ul>"
        for case in result["readiness"]
    )
    errors = "".join(
        f"<li>{esc(job['symbol'])}: {esc(reason)}</li>"
        for job in result["jobs"]
        for reason in job["errors"]
    )
    body = (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Phase 3 equity evidence audit</title><style>"
        "body{font:16px/1.6 system-ui;margin:40px auto;padding:0 24px;max-width:1100px;"
        "color:#172839}table{border-collapse:collapse;width:100%}"
        "td,th{padding:8px;text-align:left;border-bottom:1px solid #ddd}</style>"
        "<h1>Phase 3 equity evidence audit</h1>"
        f"<p>Evidence: <strong>{esc(result['status'])}</strong>. "
        f"{sum(row['rows'] for row in result['symbols']):,} equity minute bars; "
        f"{len(result['symbols'])} symbols; {len(result['jobs'])} discovery chunks.</p>"
        f"<p>Permitted interval (start, end]: {esc(result['start'])} → {esc(result['end'])}. "
        f"Generated {esc(result['generated_at'])}. Historical feed: registered Alpaca IEX.</p>"
        f"<p>{esc(result['note'])} No strategy run is started by this audit.</p>"
        f"<p>Research attempt history unchanged: {result['attempts_unchanged']}. "
        f"<a href='{esc(output.name)}'>Full evidence JSON</a> · "
        f"<a href='{esc(output.with_suffix('.csv').name)}'>Session gap CSV</a></p>"
        f"<ul>{errors}</ul><table><thead><tr><th>Symbol</th><th>Bars</th><th>Missing minutes</th>"
        "<th>Coverage</th><th>Coverage requirement</th><th>Evidence</th></tr></thead>"
        f"<tbody>{rows}</tbody></table><h2>Current experiment input requirements</h2>{cases}</html>"
    )
    with atomic_path(output.with_suffix(".html")) as temporary:
        temporary.write_text(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "run", "audit"])
    parser.add_argument("--suite", default="phase3-first-experiments-v1")
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("XASSET_DATA_DIR", "data")))
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    registry = Registry(args.data_dir)
    try:
        design = plan(registry, args.suite, args.symbols)
        if args.command == "plan":
            result = design
        elif args.command == "audit":
            result = audit(registry, design)
        else:
            with httpx.Client(
                timeout=45, headers={"User-Agent": "xasset/0.1 equity-backfill"}
            ) as client:
                result = run(registry, design, client)
        if args.output:
            if args.command == "audit":
                publish_audit(args.output, result)
            else:
                write_json(args.output, result)
        print(json.dumps(result, indent=2))
        return 0 if args.command == "plan" or result["status"] in {"downloaded", "verified"} else 1
    except (ValueError, OSError, RuntimeError, httpx.HTTPError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
