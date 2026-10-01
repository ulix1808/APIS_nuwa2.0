"""Unit tests for nuwa_serper_search (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "cdk" / "lambdas"
sys.path.insert(0, str(ROOT))

from nuwa_serper_search import (  # noqa: E402
    build_keyword_group_query,
    build_name_only_query,
    enrich_with_html,
    hit_mentions_subject,
    name_phrase_in,
    select_seeds,
    should_omit_host,
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
