#!/usr/bin/env python3
"""
Carga inicial de JSON compactos CJF → Postgres (tablas cjf_*).

Uso (desde repo APIs):
  export PGPASSWORD='...'
  # PGHOST/PGDATABASE/PGUSER ya en .env
  python3 scripts/ingest_cjf_json.py /Users/ulix/Downloads/jsoncompacts
  python3 scripts/ingest_cjf_json.py /path/to/jsons --limit 1000

Requiere: pip install 'psycopg[binary]'
Aplica primero: ./scripts/apply_migrations.sh
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    print("Instala psycopg: pip install 'psycopg[binary]'", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]


def load_env() -> None:
    for f in (ROOT / ".env", ROOT / "scripts" / "pg.env"):
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'").strip('"')
            os.environ.setdefault(k, v)


def normalize_name(value: str) -> str:
    if not value:
        return ""
    nfd = unicodedata.normalize("NFD", value)
    no_acc = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    cleaned = re.sub(r"[^a-z0-9\s]", "", no_acc.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def as_str(v) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    return None


def parse_compact(raw: dict, fallback_id: str | None):
    documento_id = as_str(raw.get("documento_id")) or (fallback_id.strip() if fallback_id else None)
    if not documento_id:
        return None
    tabla = raw.get("tabla") if isinstance(raw.get("tabla"), dict) else {}

    def t(key: str):
        return as_str(tabla.get(key))

    doc = {
        "documento_id": documento_id,
        "url_sise": as_str(raw.get("documento_url_sise")),
        "tema": t("tema"),
        "sintesis": t("sintesis"),
        "numero_expediente": t("numero_expediente"),
        "materia": t("materia"),
        "fecha_sentencia": t("fecha_sentencia"),
        "tipo_asunto": t("tipo_asunto"),
        "tipo_organo": t("tipo_organo"),
        "circuito": t("circuito"),
        "especialidad_organo": t("especialidad_organo"),
        "asunto_neun_id": t("asunto_neun_id"),
        "numero_orden": t("numero_orden"),
        "sintesis_orden": t("sintesis_orden"),
        "datos_generales": t("datos_generales"),
        "documento_disponible": t("documento_disponible"),
        "procesado_en": as_str(raw.get("procesado_en")),
    }
    mentions = []
    seen = set()
    for s in raw.get("sujetos_empresas_mencionados") or []:
        if not isinstance(s, dict):
            continue
        nombre = as_str(s.get("nombre"))
        if not nombre:
            continue
        nombre_norm = normalize_name(nombre)
        if len(nombre_norm) < 2:
            continue
        tipo = as_str(s.get("tipo"))
        rol = as_str(s.get("rol"))
        key = f"{nombre_norm}|{tipo or ''}|{rol or ''}"
        if key in seen:
            continue
        seen.add(key)
        mentions.append(
            {
                "documento_id": documento_id,
                "nombre": nombre,
                "nombre_norm": nombre_norm,
                "tipo": tipo,
                "rol": rol,
                "extraccion_fuente": as_str(s.get("fuente")),
            }
        )
    return doc, mentions


DOC_SQL = """
INSERT INTO public.cjf_documents (
  documento_id, url_sise, tema, sintesis, numero_expediente, materia,
  fecha_sentencia, tipo_asunto, tipo_organo, circuito, especialidad_organo,
  asunto_neun_id, numero_orden, sintesis_orden, datos_generales,
  documento_disponible, procesado_en, updated_at
) VALUES (
  %(documento_id)s, %(url_sise)s, %(tema)s, %(sintesis)s, %(numero_expediente)s,
  %(materia)s, %(fecha_sentencia)s, %(tipo_asunto)s, %(tipo_organo)s, %(circuito)s,
  %(especialidad_organo)s, %(asunto_neun_id)s, %(numero_orden)s, %(sintesis_orden)s,
  %(datos_generales)s, %(documento_disponible)s, %(procesado_en)s, NOW()
)
ON CONFLICT (documento_id) DO UPDATE SET
  url_sise = EXCLUDED.url_sise,
  tema = EXCLUDED.tema,
  sintesis = EXCLUDED.sintesis,
  numero_expediente = EXCLUDED.numero_expediente,
  materia = EXCLUDED.materia,
  fecha_sentencia = EXCLUDED.fecha_sentencia,
  tipo_asunto = EXCLUDED.tipo_asunto,
  tipo_organo = EXCLUDED.tipo_organo,
  circuito = EXCLUDED.circuito,
  especialidad_organo = EXCLUDED.especialidad_organo,
  asunto_neun_id = EXCLUDED.asunto_neun_id,
  numero_orden = EXCLUDED.numero_orden,
  sintesis_orden = EXCLUDED.sintesis_orden,
  datos_generales = EXCLUDED.datos_generales,
  documento_disponible = EXCLUDED.documento_disponible,
  procesado_en = EXCLUDED.procesado_en,
  updated_at = NOW()
"""

MENT_SQL = """
INSERT INTO public.cjf_mentions (
  documento_id, nombre, nombre_norm, tipo, rol, extraccion_fuente
) VALUES (
  %(documento_id)s, %(nombre)s, %(nombre_norm)s, %(tipo)s, %(rol)s, %(extraccion_fuente)s
)
"""


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("source_dir", nargs="?", default=os.environ.get("CJF_JSON_SOURCE_DIR", ""))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=200)
    args = ap.parse_args()
    if not args.source_dir:
        print("source_dir requerido", file=sys.stderr)
        return 1
    if not os.environ.get("PGPASSWORD"):
        print("export PGPASSWORD=... antes de correr", file=sys.stderr)
        return 1

    source = Path(args.source_dir)
    files = sorted(source.glob("*.json"))
    if args.limit > 0:
        files = files[: args.limit]
    print(f"files={len(files)} host={os.environ.get('PGHOST')} db={os.environ.get('PGDATABASE')}")

    conninfo = (
        f"host={os.environ['PGHOST']} port={os.environ.get('PGPORT', '5432')} "
        f"dbname={os.environ['PGDATABASE']} user={os.environ['PGUSER']} "
        f"password={os.environ['PGPASSWORD']} sslmode={os.environ.get('PGSSLMODE', 'require')}"
    )

    docs_map: dict[str, dict] = {}
    mentions: list[dict] = []
    skipped = 0
    for fp in files:
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            parsed = parse_compact(raw, fp.stem)
            if not parsed:
                skipped += 1
                continue
            doc, ments = parsed
            docs_map[doc["documento_id"]] = doc
            mentions.extend(ments)
        except Exception as e:  # noqa: BLE001
            skipped += 1
            if skipped <= 10:
                print(f"skip {fp.name}: {e}")

    docs = list(docs_map.values())
    doc_ids = list(docs_map.keys())
    print(f"parsed docs={len(docs)} mentions={len(mentions)} skipped={skipped}")

    batch = max(50, args.batch)
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        # ensure extension/tables exist (migration should already)
        with conn.cursor() as cur:
            for i in range(0, len(docs), batch):
                slice_docs = docs[i : i + batch]
                cur.executemany(DOC_SQL, slice_docs)
                print(f"  docs upserted {i + len(slice_docs)}/{len(docs)}")
            # replace mentions for these docs
            for i in range(0, len(doc_ids), batch):
                ids = doc_ids[i : i + batch]
                cur.execute("DELETE FROM public.cjf_mentions WHERE documento_id = ANY(%s)", [ids])
            for i in range(0, len(mentions), batch):
                slice_m = mentions[i : i + batch]
                cur.executemany(MENT_SQL, slice_m)
                print(f"  mentions inserted {i + len(slice_m)}/{len(mentions)}")
        conn.commit()
        stats = conn.execute(
            "SELECT (SELECT COUNT(*) FROM public.cjf_documents) AS d, "
            "(SELECT COUNT(*) FROM public.cjf_mentions) AS m"
        ).fetchone()
        print(f"DONE table counts documents={stats['d']} mentions={stats['m']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
