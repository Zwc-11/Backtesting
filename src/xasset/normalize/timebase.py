from datetime import UTC, datetime


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


def minute_floor(value: datetime) -> datetime:
    return utc(value).replace(second=0, microsecond=0)
