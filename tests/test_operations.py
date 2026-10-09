import importlib.util
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xasset.crosscheck.reconcile import paper_statement, reconcile
from xasset.research.contracts import Trade
from xasset.store.writer import writer_lock

spec = importlib.util.spec_from_file_location("backup", Path("scripts/backup.py"))
backup_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_module)


def trade():
    at = datetime(2026, 7, 1, 14, tzinfo=UTC)
    return Trade(
        id="first",
        symbol="SPY",
        fold="one",
        signal_at=at,
        entry_at=at + timedelta(minutes=1),
        exit_at=at + timedelta(minutes=10),
        entry_bar_end=at + timedelta(minutes=2),
        exit_bar_end=at + timedelta(minutes=11),
        entry_price=100,
        exit_price=101,
        units=10,
        entry_notional=1000,
        exit_notional=1010,
        gross_pnl=10,
        cost=1,
        exit_reason="time",
    )


def test_reconciliation_exposes_fill_cost_and_timing_differences():
    native = trade()
    external = native.model_copy(
        update={"cost": 2, "entry_at": native.entry_at + timedelta(minutes=1)}
    )
    result = reconcile([native], [external])
    assert not result["ok"] and not result["accepted"]
    assert {d["field"] for d in result["differences"]} == {"entry_at", "cost"}
    assert result["native_net_pnl"] == 9 and result["external_net_pnl"] == 8
    assert reconcile([native], [native])["ok"]
    assert not reconcile([], [])["ok"]
    assert reconcile([native], [])["differences"][0]["field"] == "presence"
    with pytest.raises(ValueError, match="unique"):
        reconcile([native, native], [external])


def test_paper_audit_cannot_promote_short_history_or_real_accounts(tmp_path):
    path = tmp_path / "fills.csv"
    header = "fill_id,account,symbol,session,side,units,reference_price,fill_price,commission\n"
    path.write_text(header + "one,DU123,SPY,2026-07-01,BUY,10,100,100.1,1\n")
    result = paper_statement(path, {"SPY"})
    assert result["mean_cost_bps"] == pytest.approx(20)
    assert result["status"] == "collecting" and not result["accepted"]
    with pytest.raises(ValueError, match="30"):
        paper_statement(path, {"SPY"}, 1)
    path.write_text(header + "one,U123,SPY,2026-07-01,BUY,10,100,100.1,1\n")
    with pytest.raises(ValueError, match="paper account"):
        paper_statement(path, {"SPY"})
    path.write_text(header + "one,DU123,SPY,2026-07-04,BUY,10,100,100.1,1\n")
    with pytest.raises(ValueError, match="scheduled"):
        paper_statement(path, {"SPY"})


def test_backup_restores_verified_contents_and_rejects_corruption(tmp_path):
    root, archive = tmp_path / "data", tmp_path / "backup.tar.gz"
    root.mkdir()
    (root / "registry.duckdb").write_bytes(b"synthetic database evidence")
    (root / "nested").mkdir()
    (root / "nested/input.parquet").write_bytes(b"synthetic observations")
    assert backup_module.backup(root, archive)["verified"]
    restored = tmp_path / "restored"
    assert backup_module.restore(archive, restored)["files"] == 2
    assert (restored / "registry.duckdb").read_bytes() == (root / "registry.duckdb").read_bytes()
    with pytest.raises(ValueError, match="new empty"):
        backup_module.restore(archive, restored)
    with tarfile.open(archive) as bundle:
        assert ".writer.lock" not in bundle.getnames()
    archive.write_bytes(archive.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        backup_module.verify(archive)


def test_backup_refuses_active_monitor_and_symlink_directories(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    with writer_lock(root / "monitor/daemon"):
        with pytest.raises(RuntimeError, match="Another recorder"):
            backup_module.backup(root, tmp_path / "active.tar.gz")
    (root / "unexpected").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        backup_module.backup(root, tmp_path / "symlink.tar.gz")
    assert not (tmp_path / "symlink.tar.gz").exists()


def test_backup_refuses_unsafe_archive_members_even_with_matching_manifest(tmp_path):
    import io

    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("../escape")
        member.size = 4
        bundle.addfile(member, io.BytesIO(b"test"))
    backup_module.manifest_path(archive).write_text(
        json.dumps(
            {"archive_sha256": backup_module.checksum(archive), "files": {"../escape": "arbitrary"}}
        )
    )
    with pytest.raises(ValueError, match="Unsafe"):
        backup_module.restore(archive, tmp_path / "restored")
    assert not (tmp_path / "escape").exists()
