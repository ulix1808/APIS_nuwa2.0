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
