"""Checksum-verified, atomic cache for exact provider responses."""

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CachedResponse:
    payload: object
    source: str
    query_time: str
    provider_snapshot_at: str | None
    checksum: str
    response_headers: dict[str, str] | None = None


def write_cached_response(
    path: Path,
    *,
    payload_bytes: bytes,
    source: str,
    query_time: datetime | str,
    provider_snapshot_at: str | None,
    response_headers: dict[str, str] | None = None,
) -> CachedResponse:
    """Atomically preserve exact response bytes plus retrieval provenance."""

    payload = json.loads(payload_bytes)
    checksum = sha256(payload_bytes).hexdigest()
    envelope = {
        "source": source,
        "query_time": (
            query_time.isoformat() if isinstance(query_time, datetime) else query_time
        ),
        "provider_snapshot_at": provider_snapshot_at,
        "checksum": checksum,
        "response_headers": response_headers or {},
        "payload_text": payload_bytes.decode("utf-8"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        json.dump(envelope, temporary, separators=(",", ":"), sort_keys=True)
        temporary.flush()
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)
    return CachedResponse(
        payload=payload,
        source=source,
        query_time=str(envelope["query_time"]),
        provider_snapshot_at=provider_snapshot_at,
        checksum=checksum,
        response_headers=response_headers,
    )


def read_cached_response(path: Path) -> CachedResponse:
    """Read and checksum-validate one cached provider response."""

    envelope = json.loads(path.read_text())
    if not isinstance(envelope, dict):
        raise ValueError(f"invalid cache envelope: {path}")
    payload_text = envelope.get("payload_text")
    checksum = envelope.get("checksum")
    if not isinstance(payload_text, str) or not isinstance(checksum, str):
        raise ValueError(f"invalid cache envelope: {path}")
    if sha256(payload_text.encode()).hexdigest() != checksum:
        raise ValueError(f"cache checksum mismatch: {path}")
    raw_headers = envelope.get("response_headers", {})
    if not isinstance(raw_headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_headers.items()
    ):
        raise ValueError(f"invalid cache response headers: {path}")
    return CachedResponse(
        payload=json.loads(payload_text),
        source=str(envelope["source"]),
        query_time=str(envelope["query_time"]),
        provider_snapshot_at=(
            str(envelope["provider_snapshot_at"])
            if envelope.get("provider_snapshot_at") is not None
            else None
        ),
        checksum=checksum,
        response_headers={str(key): str(value) for key, value in raw_headers.items()},
    )


def write_json_atomic(path: Path, value: Any) -> None:
    """Write generated metadata atomically."""

    encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        temporary.write(encoded)
        temporary.flush()
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)
