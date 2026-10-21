"""Unit tests for nuwa_serper_search (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "cdk" / "lambdas"
sys.path.insert(0, str(ROOT))

from nuwa_serper_search import (  # noqa: E402
    SerperUpstreamError,
    build_keyword_group_query,
    build_name_only_query,
    enrich_with_html,
    hit_mentions_subject,
    name_phrase_in,
    run_serper_goog_search,
    search_organic,
    select_seeds,
    should_omit_host,
    unique_subject_names,
)


def test_name_only_and_keyword_queries():
    assert build_name_only_query("  Ana Pérez ") == '"Ana Pérez"'
    assert (
        build_keyword_group_query("Ana Pérez", ["juicio", "corrupción", "detenido"])
        == '"Ana Pérez" AND ("juicio" OR "corrupción" OR "detenido")'
    )


def test_name_phrase_accent_fold():
    assert name_phrase_in("Caso de Isidro Faine Casas", "Isidro Fainé Casas")
    assert not name_phrase_in("Otro sujeto", "Natalia Lucinda Pacheco Chaves")


def test_hit_mentions_trellis_snippet():
    assert hit_mentions_subject(
        "Natalia Lucinda Pacheco Chaves",
        {
            "title": "Cases filed",
            "description": "NATALIA LUCINDA PACHECO CHAVES vs NICHOLE WIEPERT Malpractice",
            "link": "https://trellis.law/x",
        },
    )


def test_omit_facebook():
    assert should_omit_host("https://www.facebook.com/foo")
    assert not should_omit_host("https://trellis.law/coverage/x")


def test_omit_directories_and_social_by_whole_label():
    for url in (
        "https://www.aliadojudicial.com/directorio/co/camara-comercio/nombres/hernandez-hernandez-joel",
        "https://x.com/someone/status/1",
        "https://m.facebook.com/story.php?id=1",
        "https://www.amazon.com.mx/dp/1",
        "https://youtu.be/abc",
    ):
        assert should_omit_host(url), url
    for url in ("https://www.fox.com/news", "https://www.dropbox.com/s/a.pdf", "https://www.foxnews.com/x"):
        assert not should_omit_host(url), url


def test_select_seeds_and_snippet_adverse_without_html():
    hits = [
        {
            "title": "Cases",
            "link": "https://trellis.law/a",
            "description": "NATALIA LUCINDA PACHECO CHAVES vs WIEPERT Medical Malpractice",
            "engine": "google",
            "querySource": "name_only",
        },
        {
            "title": "Narco",
            "link": "https://example.com/narco",
            "description": "carteles unidos",
            "engine": "google",
            "querySource": "keyword",
        },
        {
            "title": "FB",
            "link": "https://www.facebook.com/x",
            "description": "Natalia Lucinda Pacheco Chaves acusada",
            "engine": "google",
            "querySource": "keyword",
        },
    ]
    seeds = select_seeds("Natalia Lucinda Pacheco Chaves", hits)
    assert any("trellis" in h["link"] for h in seeds)
    enriched = enrich_with_html("Natalia Lucinda Pacheco Chaves", seeds, fetch_html=False)
    assert len(enriched) == 1
    assert enriched[0]["keepReason"] == "snippet+adverse"
    assert "trellis" in enriched[0]["link"]


def test_run_serper_fails_loud_on_http_403(monkeypatch):
    """Invalid Serper key must not return success with empty hits (BFF would miss Trellis)."""
    monkeypatch.setenv("SERPER_API_KEY", "invalid-key-for-test")

    def boom(*_args, **_kwargs):
        raise SerperUpstreamError("HTTP Error 403: Forbidden", status=403)

    monkeypatch.setattr("nuwa_serper_search._serper_post", boom)
    result = run_serper_goog_search("Natalia Lucinda Pacheco Chaves", fetch_html=False)
    assert result["success"] is False
    assert result["code"] == "SERPER_UPSTREAM_FORBIDDEN"
    assert result["hits"] == []
    assert result["meta"]["http403"] >= 1
    assert result["meta"]["okJobs"] == 0


def test_unique_subject_names_dedupes_orders():
    names = unique_subject_names(
        "Juan Pérez García",
        ["Pérez García Juan", "juan perez garcia", "Juan Pérez García"],
    )
    assert names == ["Juan Pérez García", "Pérez García Juan"]


def test_search_organic_builds_jobs_for_all_variants(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    seen_q: list[str] = []

    def fake_post(_key, q, _gl, _hl, _num):
        seen_q.append(q)
        return []

    monkeypatch.setattr("nuwa_serper_search._serper_post", fake_post)
    subjects = ["Natalia Lucinda Pacheco Chaves", "Pacheco Chaves Natalia Lucinda"]
    _hits, stats = search_organic("test-key", subjects, extra_keywords=None)
    assert stats["subjectCount"] == 2
    assert stats["okJobs"] == stats["jobCount"]
    # Name-only ES+EN for each variant (at least 4); keyword jobs multiply by subjects.
    assert stats["jobCount"] > 4
    assert any('"Natalia Lucinda Pacheco Chaves"' == q for q in seen_q)
    assert any('"Pacheco Chaves Natalia Lucinda"' == q for q in seen_q)
    # Single-subject baseline must be ~half the multi-variant job count.
    seen_q.clear()
    _hits1, stats1 = search_organic("test-key", ["Natalia Lucinda Pacheco Chaves"])
    assert stats["jobCount"] == stats1["jobCount"] * 2


def test_run_serper_with_subject_variants_one_invocation(monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    call_count = {"n": 0}

    def fake_post(_key, q, _gl, _hl, _num):
        call_count["n"] += 1
        if "Pacheco Chaves Natalia" in q and "AND" not in q:
            return [
                {
                    "title": "Cases filed",
                    "link": "https://trellis.law/inverted",
                    "snippet": "PACHECO CHAVES NATALIA LUCINDA vs WIEPERT Malpractice",
                }
            ]
        return []

    monkeypatch.setattr("nuwa_serper_search._serper_post", fake_post)
    result = run_serper_goog_search(
        "Natalia Lucinda Pacheco Chaves",
        fetch_html=False,
        subject_variants=["Pacheco Chaves Natalia Lucinda"],
    )
    assert result["success"] is True
    assert result["meta"]["subjectCount"] == 2
    assert call_count["n"] == result["meta"]["jobCount"]
    assert any("trellis.law/inverted" in (h.get("link") or "") for h in result["hits"])


def test_rank_hits_keyword_hits_naming_subject_first():
    from nuwa_serper_search import rank_hits

    s = ["Abelardo Maldonado Rosado"]
    out = rank_hits(
        s,
        [
            {"title": "Notaría 16", "link": "https://a.mx/notaria", "description": "Abelardo Maldonado Rosado notario", "querySource": "name_only"},
            {"title": "Otra nota", "link": "https://b.mx/x", "description": "sin el nombre", "querySource": "keyword"},
            # First seen with a snippet that does not name him; a later query's snippet does.
            {"title": "Surge nueva acusación contra Obispo", "link": "https://diario.mx/obispo", "description": "Ramón Castro, obispo…", "querySource": "keyword"},
            {"title": "Surge nueva acusación contra Obispo", "link": "https://diario.mx/obispo", "description": "… Abelardo Maldonado Rosado, habrían cometido los ilícitos", "querySource": "keyword"},
            {"title": "Perfil", "link": "https://c.mx/perfil", "description": "Abelardo Maldonado Rosado", "querySource": "name_only"},
            # Same URL (other case) from a keyword query: it now counts as a keyword hit too.
            {"title": "dup", "link": "https://A.mx/notaria", "description": "", "querySource": "keyword"},
        ],
    )
    assert [h["link"] for h in out] == [
        "https://a.mx/notaria",
        "https://diario.mx/obispo",
        "https://c.mx/perfil",
        "https://b.mx/x",
    ]
    assert "Maldonado Rosado" in out[1]["description"]
    assert out[0]["querySource"] == "name_only"
    assert not any(k.startswith("_") for h in out for k in h)


def test_keywords_cover_criminal_complaints_and_cap_is_60():
    from nuwa_serper_search import ADVERSE_KEYWORDS_ES, MAX_SERPER_HITS

    for k in ("falsedad", "falsificación", "denuncia", "denunciado", "querella", "acusados"):
        assert k in ADVERSE_KEYWORDS_ES
    assert MAX_SERPER_HITS == 60


def _http_error(code: int, retry_after: str | None = None):
    from email.message import Message
    from urllib.error import HTTPError

    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("https://google.serper.dev/search", code, "x", headers, None)


def test_serper_retries_429_and_5xx_then_succeeds(monkeypatch):
    import nuwa_serper_search as ns

    calls = []
    outcomes = [_http_error(429, "1"), _http_error(503), [{"title": "ok"}]]

    def once(*_a):
        calls.append(1)
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    sleeps = []
    monkeypatch.setattr(ns, "_serper_post_once", once)
    assert ns._serper_post("k", "q", "mx", "es", 10, sleep=sleeps.append) == [{"title": "ok"}]
    assert len(calls) == 3
    assert sleeps[0] == 1.0  # Retry-After from Serper
    assert 1.5 <= sleeps[1] < 1.75  # second backoff step


def test_serper_does_not_retry_4xx_and_gives_up_after_4(monkeypatch):
    import nuwa_serper_search as ns

    monkeypatch.setattr(ns, "_serper_post_once", lambda *_a: (_ for _ in ()).throw(_http_error(403)))
    try:
        ns._serper_post("k", "q", "mx", "es", 10, sleep=lambda _s: None)
        raise AssertionError("expected SerperUpstreamError")
    except SerperUpstreamError as ex:
        assert ex.status == 403

    calls = []

    def always_429(*_a):
        calls.append(1)
        raise _http_error(429)

    monkeypatch.setattr(ns, "_serper_post_once", always_429)
    try:
        ns._serper_post("k", "q", "mx", "es", 10, sleep=lambda _s: None)
        raise AssertionError("expected SerperUpstreamError")
    except SerperUpstreamError as ex:
        assert ex.status == 429
    assert len(calls) == ns.SERPER_ATTEMPTS
