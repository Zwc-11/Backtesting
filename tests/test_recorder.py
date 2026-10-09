import json
from datetime import UTC, datetime, timedelta

import httpx
import polars as pl
import pytest
from conftest import chart_payload

from xasset.config import Instrument
from xasset.ingest.yahoo_recorder import FeedError, fetch, normalize
from xasset.recorder import record
from xasset.store.writer import load_bars

START = datetime(2026, 10, 7, 13, 30, tzinfo=UTC)


def test_normalization_excludes_unsettled_missing_off_session_and_terminal_quotes(instrument):
    times = [
        START - timedelta(minutes=1),
        START,
        START + timedelta(minutes=1),
        START + timedelta(minutes=2),
        START + timedelta(seconds=150),
    ]
    payload = chart_payload("TEST", times, close=[101, 101, None, 101, 101])
    result = normalize(
        payload,
        instrument,
        START - timedelta(hours=1),
        START + timedelta(hours=1),
        START + timedelta(minutes=23),
    )
    assert result.null_rows == 1
    assert result.excluded_rows == 2
    assert result.bars["ts_end"].to_list() == [
        START + timedelta(minutes=1),
        START + timedelta(minutes=3),
    ]
    assert result.bars["ts_end"].dtype == pl.Datetime("us", "UTC")
    # At this cutoff the third bar has not settled yet.
    early = normalize(
        payload, instrument, START, START + timedelta(hours=1), START + timedelta(minutes=22)
    )
    assert early.bars.height == 1


@pytest.mark.parametrize(
    "payload",
    [b"not json", b'{"chart":{"result":[]}}', b'{"chart":{"error":{"description":"no data"}}}'],
)
def test_malformed_and_error_responses_fail(payload, instrument):
    with pytest.raises(FeedError):
        normalize(
            payload, instrument, START, START + timedelta(hours=1), START + timedelta(hours=2)
        )


def test_wrong_symbol_and_misaligned_arrays_fail(instrument):
    for payload in [chart_payload("OTHER", [START]), chart_payload("TEST", [START], volume=[])]:
        with pytest.raises(FeedError):
            normalize(
                payload, instrument, START, START + timedelta(hours=1), START + timedelta(hours=2)
            )


def test_proxy_and_missing_volume_flags():
    item = Instrument(
        id="ES_FRONT",
        asset_class="futures",
        tier="B",
        venue="CME",
        session="all",
        yahoo_symbol="ES=F",
        proxy=True,
    )
    result = normalize(
        chart_payload("ES=F", [START], volume=[None]),
        item,
        START,
        START + timedelta(hours=1),
        START + timedelta(hours=2),
    )
    assert "front_month_proxy" in result.bars["flags"][0]
    assert "volume_missing" in result.bars["flags"][0]
    assert result.bars["volume"][0] is None


def test_transient_retry_and_permanent_error(monkeypatch):
    calls = []
    monkeypatch.setattr("xasset.ingest.yahoo_recorder.time.sleep", lambda _: None)

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, content=b"ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert fetch(client, "TEST", START, START + timedelta(days=1)) == b"ok"
    assert len(calls) == 2
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch(client, "TEST", START, START + timedelta(days=1))


def test_record_raw_store_catalog_idempotency_and_partial_failure(tmp_path, instrument):
    payload = chart_payload("TEST", [START, START + timedelta(minutes=1)])
    bad = instrument.model_copy(update={"id": "BAD", "yahoo_symbol": "BAD"})

    def handler(request):
        return (
            httpx.Response(403)
            if "BAD" in request.url.path
            else httpx.Response(200, content=payload)
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = record(
            tmp_path,
            [instrument, bad],
            START,
            START + timedelta(hours=1),
            client=client,
            as_of=START + timedelta(hours=2),
        )
        second = record(
            tmp_path,
            [instrument],
            START,
            START + timedelta(hours=1),
            client=client,
            as_of=START + timedelta(hours=2),
        )
    assert first["status"] == "failed"
    assert first["instruments"][0]["added_rows"] == 2
    assert first["instruments"][1]["status"] == "failed"
    assert second["status"] == "ok"
    assert second["instruments"][0]["added_rows"] == 0
    assert load_bars(tmp_path, instrument).height == 2
    assert second["coverage"][0]["rows"] == 2
    assert len(list((tmp_path / "raw").rglob("*.json"))) == 1
    assert json.loads((tmp_path / "runs/latest.json").read_text())["run_id"] == second["run_id"]
    assert len(list((tmp_path / "runs").glob("20*.json"))) == 2


def test_record_chunks_without_exceeding_seven_days(tmp_path, instrument):
    spans = []
    as_of = START + timedelta(days=16)

    def handler(request):
        first, last = (int(request.url.params[key]) for key in ("period1", "period2"))
        spans.append((first, last))
        return httpx.Response(200, content=chart_payload("TEST", []))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = record(
            tmp_path, [instrument], START, START + timedelta(days=15), client=client, as_of=as_of
        )
    assert len(spans) == 3
    assert all(end - start <= 7 * 86400 for start, end in spans)
    assert spans[0][1] == spans[1][0]
    assert spans[1][1] == spans[2][0]
    assert result["status"] == "failed"  # zero bars is not a successful capture


def test_rejects_out_of_retention_window(tmp_path, instrument):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("No network expected"))
    ) as client:
        with pytest.raises(ValueError, match="last 30 days"):
            record(
                tmp_path,
                [instrument],
                START,
                START + timedelta(days=1),
                client=client,
                as_of=START + timedelta(days=40),
            )


def test_rejects_naive_timestamps(instrument):
    with pytest.raises(ValueError, match="timezone"):
        normalize(
            chart_payload("TEST", [START]),
            instrument,
            datetime(2026, 10, 7),
            START + timedelta(hours=1),
            START + timedelta(hours=2),
        )


def test_catalog_failure_marks_run_failed_without_losing_bars(tmp_path, instrument, monkeypatch):
    def fail_catalog(_):
        raise OSError("disk unavailable")

    monkeypatch.setattr("xasset.recorder.rebuild_catalog", fail_catalog)
    payload = chart_payload("TEST", [START])
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    ) as client:
        result = record(
            tmp_path,
            [instrument],
            START,
            START + timedelta(hours=1),
            client=client,
            as_of=START + timedelta(hours=2),
        )
    assert result["status"] == "failed"
    assert "disk unavailable" in result["catalog_error"]
    assert load_bars(tmp_path, instrument).height == 1
    assert json.loads((tmp_path / "runs/latest.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("bad_field,bad_value", [("volume", 1.5), ("open", "bad")])
def test_malformed_price_and_fractional_volume_fail(instrument, bad_field, bad_value):
    payload = chart_payload("TEST", [START], **{bad_field: [bad_value]})
    with pytest.raises(FeedError):
        normalize(
            payload, instrument, START, START + timedelta(hours=1), START + timedelta(hours=2)
        )
