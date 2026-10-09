"""One explicit holdout release per immutable family; no automatic unlock."""

import json
from datetime import UTC, datetime
from typing import Any

from xasset.research.trials import Registry


def open_vault(registry: Registry, family: str, discovery_run: str, reason: str) -> dict[str, Any]:
    if len(reason.strip()) < 10:
        raise ValueError("Record a substantive reason for the one-time holdout release")
    with registry.connection() as db:
        row = db.execute("SELECT vault_run FROM families WHERE family = ?", [family]).fetchone()
        if row is None:
            raise ValueError("Unknown experiment family")
        if row[0] is not None:
            raise ValueError("This family's vault has already been opened")
        if db.execute(
            "SELECT 1 FROM runs WHERE family = ? AND status = 'running'", [family]
        ).fetchone():
            raise ValueError("Cannot release a vault while the family has an active run")
        run = db.execute(
            "SELECT family, phase, status, result_json FROM runs WHERE run_id = ?", [discovery_run]
        ).fetchone()
        if (
            run is None
            or run[0] != family
            or run[1] != "discovery"
            or run[2] != "completed"
            or not run[3]
            or json.loads(run[3]).get("gate", {}).get("status") != "candidate"
        ):
            raise ValueError(
                "Vault release requires a completed candidate discovery run for this family"
            )
        latest = db.execute(
            "SELECT run_id FROM runs WHERE family = ? ORDER BY started_at DESC LIMIT 1", [family]
        ).fetchone()
        if latest is None or latest[0] != discovery_run:
            raise ValueError(
                "Release must reference the latest discovery attempt, not an older winner"
            )
        opened = datetime.now(UTC)
        db.execute(
            "UPDATE families SET vault_run = ?, vault_reason = ?, vault_opened_at = ? "
            "WHERE family = ?",
            [discovery_run, reason.strip(), opened, family],
        )
    return {
        "family": family,
        "discovery_run": discovery_run,
        "opened_at": opened.isoformat(),
        "status": "opened",
        "remaining_attempts": 1,
    }


def abandon(registry: Registry, run_id: str, reason: str) -> None:
    if len(reason.strip()) < 10:
        raise ValueError("Record why this interrupted run is being abandoned")
    registry.finish(run_id, "failed", error="Explicitly abandoned: " + reason.strip())
