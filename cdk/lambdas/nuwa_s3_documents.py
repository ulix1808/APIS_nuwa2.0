"""S3: prefijo por cliente y presigned URLs."""

from __future__ import annotations

import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

from document_helpers import keep_marker_key, presign_ttl_seconds


def _bucket() -> str:
    b = (os.environ.get("NUWA_DOCUMENTS_BUCKET") or "").strip()
    if not b:
        raise RuntimeError("NUWA_DOCUMENTS_BUCKET no configurado")
    return b


def _s3():
    return boto3.client("s3")


def ensure_client_storage_prefix(client_id: int, *, initialized_by: str = "system") -> dict[str, Any]:
    bucket = _bucket()
    prefix = f"clients/{client_id}/"
    key = keep_marker_key(client_id)
    s3 = _s3()
    already = False
    try:
        s3.head_object(Bucket=bucket, Key=key)
        already = True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "403"):
            raise
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=b"",
            Metadata={"createdBy": initialized_by, "purpose": "client-root"},
        )
    return {"clientId": client_id, "s3Prefix": prefix, "initialized": True, "alreadyExists": already}


def head_object(s3_key: str) -> dict[str, Any] | None:
    try:
        return _s3().head_object(Bucket=_bucket(), Key=s3_key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return None
        raise


def delete_object(s3_key: str) -> None:
    _s3().delete_object(Bucket=_bucket(), Key=s3_key)


def presigned_put_url(*, s3_key: str, mime_type: str) -> tuple[str, dict[str, str], int]:
    ttl = presign_ttl_seconds()
    params = {"Bucket": _bucket(), "Key": s3_key, "ContentType": mime_type}
    url = _s3().generate_presigned_url("put_object", Params=params, ExpiresIn=ttl, HttpMethod="PUT")
    return url, {"Content-Type": mime_type}, ttl


def presigned_get_url(*, s3_key: str) -> tuple[str, int]:
    ttl = presign_ttl_seconds()
    url = _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": s3_key},
        ExpiresIn=ttl,
    )
    return url, ttl
