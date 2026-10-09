import csv
import hashlib
import io
import lzma
from datetime import UTC, datetime, timedelta
from zipfile import ZipFile

import httpx
import pytest

from xasset.config import Instrument
from xasset.ingest import alpaca, binance_archive, dukascopy
from xasset.ingest.base import download
from xasset.ingest.history import ingest
from xasset.qc.checks import check_bars
from xasset.store.writer import load_bars

START = datetime(2026, 9, 1, tzinfo=UTC)
FILENAME = "BTCUSDT-1m-2026-09.zip"


def crypto(market="spot"):
    return Instrument(
        id="BTC_USDT",
        asset_class="crypto",
        tier="A",
        venue="BINANCE",
        session="all",
        sources=["binance"],
        binance_symbol="BTCUSDT",
        binance_market=market,
    )


def fx():
    return Instrument(
        id="EURUSD",
        asset_class="fx",
        tier="A",
        venue="DUKASCOPY",
        session="all",
        sources=["dukascopy"],
        dukascopy_symbol="EURUSD",
        price_scale=100000,
    )


def archive(unit=1_000_000, header=False, close_offset=-1):
    opened = int(START.timestamp()) * unit
    text = io.StringIO()
    writer = csv.writer(text)
    if header:
        writer.writerow(
            [
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
                "quote_volume",
                "count",
                "buy_volume",
                "buy_quote",
                "ignore",
            ]
        )
    writer.writerow(
        [
            opened,
            100,
            104,
            98,
            102,
            0.125,
            opened + 60 * unit + close_offset,
            12.75,
            2,
            0.1,
            10.2,
            0,
        ]
    )
    data = io.BytesIO()
    with ZipFile(data, "w") as output:
        output.writestr(FILENAME.replace(".zip", ".csv"), text.getvalue())
    payload = data.getvalue()
    checksum = f"{hashlib.sha256(payload).hexdigest()}  {FILENAME}\n".encode()
    return payload, checksum


@pytest.mark.parametrize("unit,header", [(1000, False), (1_000_000, False), (1000, True)])
def test_binance_timestamp_units_headers_and_fractional_volume(unit, header):
    payload, checksum = archive(unit, header)
    bars = binance_archive.decode_archive(payload, checksum, FILENAME, crypto())
    assert check_bars(bars).ok
    assert bars["volume"][0] == 0.125
    assert bars["ts_end"][0] == START + timedelta(minutes=1)


def test_binance_checksum_filename_and_candle_bounds():
    payload, checksum = archive()
    for data, check in [
        (payload + b"corrupt", checksum),
        (payload, checksum.replace(b"BTCUSDT", b"ETHUSDT")),
    ]:
        with pytest.raises(ValueError, match="checksum"):
            binance_archive.decode_archive(data, check, FILENAME, crypto())
    payload, checksum = archive(close_offset=0)
    with pytest.raises(ValueError, match="timestamp bounds"):
        binance_archive.decode_archive(payload, checksum, FILENAME, crypto())


def test_binance_ingest_end_to_end_is_repeatable_and_keeps_evidence(tmp_path):
    payload, checksum = archive()

    def handler(request):
        assert "/spot/monthly/klines/BTCUSDT/1m/" in request.url.path
        return httpx.Response(
            200, content=checksum if request.url.path.endswith("CHECKSUM") else payload
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = ingest(
            tmp_path,
            [crypto()],
            "binance",
            START,
            START + timedelta(days=1),
            client=client,
            as_of=datetime(2026, 10, 8, tzinfo=UTC),
        )
        second = ingest(
            tmp_path,
            [crypto()],
            "binance",
            START,
            START + timedelta(days=1),
            client=client,
            as_of=datetime(2026, 10, 8, tzinfo=UTC),
        )
    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert second["instruments"][0]["added_rows"] == 0
    assert load_bars(tmp_path, crypto())["volume"][0] == 0.125
    assert len(list((tmp_path / "raw/binance/BTC_USDT").glob("*"))) == 2
    assert (tmp_path / "sources/binance/bars/crypto/BTC_USDT/2026-09.parquet").exists()


def test_corrupt_download_cannot_enter_store(tmp_path):
    payload, checksum = archive()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=checksum if request.url.path.endswith("CHECKSUM") else payload + b"bad"
            )
        )
    ) as client:
        result = ingest(
            tmp_path,
            [crypto()],
            "binance",
            START,
            START + timedelta(days=1),
            client=client,
            as_of=datetime(2026, 10, 8, tzinfo=UTC),
        )
    assert result["status"] == "failed"
    assert load_bars(tmp_path, crypto()).is_empty()


def test_current_month_rejected_before_download():
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no request"))) as client:
        with pytest.raises(ValueError, match="completed calendar months"):
            list(
                binance_archive.batches(
                    client, crypto(), START, START + timedelta(days=2), START + timedelta(days=10)
                )
            )


def test_dukascopy_preserves_quotes_midpoint_and_missing_minutes():
    ticks = b"".join(
        dukascopy.TICK.pack(*row)
        for row in [
            (0, 110020, 110000, 3.0, 4.0),
            (1000, 110040, 110010, 3.0, 4.0),
            (120000, 110010, 109990, 3.0, 4.0),
        ]
    )
    bars = dukascopy.decode_ticks(lzma.compress(ticks), START, fx())
    assert check_bars(bars).ok
    assert bars.height == 2
    assert bars["open"][0] == pytest.approx(1.1001)
    assert bars["bid_close"][0] == pytest.approx(1.1001)
    assert bars["ask_close"][0] == pytest.approx(1.1004)
    assert bars["volume"].null_count() == 2
    assert bars["ts_end"].to_list() == [START + timedelta(minutes=1), START + timedelta(minutes=3)]


@pytest.mark.parametrize(
    "tick",
    [
        (3_600_000, 110020, 110000, 1.0, 1.0),
        (0, 110000, 110020, 1.0, 1.0),
        (0, 110020, 110000, float("nan"), 1.0),
    ],
)
def test_bad_dukascopy_tick_rejected(tick):
    with pytest.raises(ValueError):
        dukascopy.decode_ticks(lzma.compress(dukascopy.TICK.pack(*tick)), START, fx())


def test_dukascopy_truncation_and_zero_based_month():
    with pytest.raises(ValueError, match="partial tick"):
        dukascopy.decode_ticks(lzma.compress(b"partial"), START, fx())
    payload = lzma.compress(dukascopy.TICK.pack(0, 110020, 110000, 1.0, 1.0))

    def handler(request):
        assert request.url.path.endswith("/2026/08/01/00h_ticks.bi5")
        return httpx.Response(200, content=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        batches = list(
            dukascopy.batches(
                client, fx(), START, START + timedelta(hours=1), START + timedelta(days=1)
            )
        )
    assert batches[0].bars.height == 1


def test_rate_limit_retry_after_is_respected(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr("xasset.ingest.base.time.sleep", sleeps.append)

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "5"})
            if len(calls) == 1
            else httpx.Response(200, content=b"ok")
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert download(client, "https://example.test") == b"ok"
    assert sleeps == [5]
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": "3600"})
        )
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            download(client, "https://example.test")
    assert sleeps == [5]


def test_alpaca_credentials_pagination_and_raw_feed(monkeypatch, instrument):
    item = instrument.model_copy(update={"alpaca_symbol": "TEST", "sources": ["alpaca"]})
    monkeypatch.setenv("ALPACA_API_KEY", "test-only-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-only-secret")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["adjustment"] == "raw"
        assert request.url.params["feed"] == "iex"
        assert request.headers["APCA-API-KEY-ID"] == "test-only-key"
        minute = "30" if len(calls) == 1 else "31"
        return httpx.Response(
            200,
            json={
                "symbol": "TEST",
                "bars": [
                    {
                        "t": f"2026-09-01T13:{minute}:00Z",
                        "o": 100,
                        "h": 102,
                        "l": 99,
                        "c": 101,
                        "v": 12,
                    }
                ],
                "next_page_token": "page2" if len(calls) == 1 else None,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        output = list(
            alpaca.batches(
                client, item, START, START + timedelta(days=1), START + timedelta(days=2)
            )
        )
    assert sum(batch.bars.height for batch in output) == 2
    assert calls[1].url.params["page_token"] == "page2"


def test_missing_alpaca_credentials_fails_before_network(monkeypatch, instrument):
    item = instrument.model_copy(update={"alpaca_symbol": "TEST", "sources": ["alpaca"]})
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no request"))) as client:
        with pytest.raises(ValueError, match="Missing environment bindings"):
            list(
                alpaca.batches(
                    client, item, START, START + timedelta(days=1), START + timedelta(days=2)
                )
            )
