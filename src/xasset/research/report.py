"""Readable reports from persisted runs, including blocked/failed attempts."""

import json
from typing import Any


def markdown(run: dict[str, Any]) -> str:
    lines = [
        f"# {run['family']} — {run['phase']} run",
        "",
        f"Run: `{run['run_id']}`",
        f"Status: **{run['status']}**",
        f"Selection trial count: {run['trial_count']}",
        "",
    ]
    if run["error"]:
        lines.extend(["## Blocker or failure", "", "```text", str(run["error"]), "```", ""])
    if run["result"] and "readiness" in run["result"]:
        lines.extend(
            [
                "## Missing prerequisites",
                "",
                *[f"- {reason}" for reason in run["result"]["readiness"]["blockers"]],
                "",
            ]
        )
    if run["result"] and "engine_result" in run["result"]:
        report = run["result"]
        lines.extend(
            [
                f"Gate: **{report['gate']['status']}** (not an accepted strategy verdict)",
                "",
                f"Input: {report['input_rows']} rows; SHA-256 `{report['data_sha256']}`",
                "",
                "```json",
                json.dumps(report["gate"], indent=2, allow_nan=False),
                "```",
                "",
            ]
        )
    return "\n".join(lines)
