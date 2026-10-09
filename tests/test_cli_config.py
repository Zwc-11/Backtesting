import json

import pytest
from pydantic import ValidationError

from xasset.cli import main
from xasset.config import Instrument, Universe, load_universe


def test_default_universe_and_unknown_symbol():
    from pathlib import Path

    instruments = load_universe(Path("config/universe.yaml"))
    assert len(instruments) == 5
    with pytest.raises(ValueError, match="Unknown instrument"):
        load_universe(Path("config/universe.yaml"), ["MISSING"])


def test_path_traversal_and_duplicate_ids_rejected(instrument):
    with pytest.raises(ValidationError):
        Instrument.model_validate({**instrument.model_dump(), "id": "../escape"})
    with pytest.raises(ValidationError):
        Universe(instruments=[instrument, instrument])


def test_cli_empty_data_reports_failure(tmp_path, capsys):
    assert main(["qc", "--data-dir", str(tmp_path)]) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert (
        main(
            [
                "health",
                "--data-dir",
                str(tmp_path),
                "--symbols",
                "SHELL_UK",
                "--as-of",
                "2026-10-08T00:00Z",
            ]
        )
        == 1
    )
    assert not json.loads(capsys.readouterr().out)["ok"]


def test_cli_empty_catalog_is_explicit(tmp_path, capsys):
    assert main(["catalog", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"coverage": []}


def test_cli_rejects_naive_date_and_nonpositive_sessions(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["record", "--end", "2026-10-07"])
    assert exc.value.code == 2
    assert main(["health", "--data-dir", str(tmp_path), "--sessions", "0"]) == 2
    assert "positive" in capsys.readouterr().err
