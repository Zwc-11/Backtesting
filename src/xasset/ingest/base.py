"""Shared historical adapter contract and bounded public HTTP downloads."""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import polars as pl


@dataclass(frozen=True)
class RawArtifact:
    payload: bytes
    suffix: str
    url: str


@dataclass(frozen=True)
class Batch:
    start: datetime
    end: datetime
    bars: pl.DataFrame
    artifacts: list[RawArtifact]
    details: dict[str, Any] = field(default_factory=dict)


def download(
    client: httpx.Client,
    url: str,
    limit: int = 64 * 1024 * 1024,
    params: dict[str, str | int] | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    for attempt in range(3):
        with client.stream("GET", url, params=params, headers=headers) as response:
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                retry = response.headers.get("retry-after", "")
                # Never retry sooner than the source requests. Large delays fail
                # this run so the host can retry later, rather than sleeping for hours.
                if retry.isdecimal() and int(retry) > 30:
                    response.raise_for_status()
                delay = float(retry) if retry.isdecimal() else 2 ** (attempt + 1)
            else:
                response.raise_for_status()
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(f"Download exceeds {limit} bytes: {url}")
                    chunks.append(chunk)
                return b"".join(chunks)
        time.sleep(delay)
    raise AssertionError("unreachable")
