from document_helpers import (
    build_index_chunks,
    client_s3_prefix,
    document_s3_key,
    mime_allowed,
    sanitize_filename,
)


def test_sanitize_filename() -> None:
    assert sanitize_filename("acta constitutiva.pdf") == "acta_constitutiva.pdf"


def test_document_s3_key() -> None:
    k = document_s3_key(123, "abc-uuid", "foo.pdf")
    assert k == "clients/123/documents/abc-uuid/foo.pdf"
    assert client_s3_prefix(123) == "clients/123/"


def test_mime_allowed() -> None:
    assert mime_allowed("application/pdf")
    assert not mime_allowed("application/x-msdownload")


def test_build_index_chunks() -> None:
    chunks = build_index_chunks(
        {
            "documentType": "Acta",
            "summary": "Resumen",
            "parties": [{"name": "PEMEX", "partyType": "organization"}],
        }
    )
    assert len(chunks) >= 2
