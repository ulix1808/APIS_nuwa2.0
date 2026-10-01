"""
SerperGoogSearch — búsqueda Google adverse-media (nombre-solo + keyword chunks)
+ filtro de seeds + enriquecimiento HTML paralelo (patrón Nuwa 1.0 / BFF 2.0).

Salida lista para ingest a Grok (pass google_serper_seeded).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
import unicodedata
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SERPER_URL = "https://google.serper.dev/search"
MAX_SERPER_HITS = 40
KEYWORD_CHUNK_SIZE = 3
SERPER_CONCURRENCY = 10
HTML_TIMEOUT_SEC = 3.0
HTML_CONCURRENCY = 20
HTML_BODY_CAP = 600_000
EXTRACT_RADIUS = 420
NUM_PER_QUERY = 10

ADVERSE_KEYWORDS_ES = [
    "Fraude", "estafa", "acusado", "prisión", "acusada", "penal", "juicio",
    "corrupción", "detenido", "detienen", "delincuente", "asesino", "homicidio",
    "asesinato", "ilegal", "abuso", "soborno", "cohecho", "delito", "beneficiado",
    "lavado de dinero", "OFAC", "sanción", "sanciones", "inhabilitado",
    "inhabilitación", "lista negra", "investigado", "imputado", "condenado",
    "sentenciado", "prófugo", "extorsión", "narcotráfico", "crimen organizado",
    "defraudación", "peculado", "EFOS", "judicial", "expedientes", "candidato",
]

ADVERSE_KEYWORDS_EN = [
    "Fraud", "corruption", "money laundering", "criminal", "offender", "accused",
    "detained", "arrested", "killer", "assassin", "homicide", "murder", "illegal",
    "bribe", "bribery", "sanction", "sanctions", "debarred", "debarment",
    "blacklist", "defendant", "indicted", "convicted", "fugitive", "extortion",
    "organized crime", "drug trafficking", "embezzlement", "unicourt", "violation",
]

OMIT_HOST_FRAGMENTS = (
    "amazon.", "linkedin.", "seccionamarilla.", "idcrawl.", "facebook.",
    "instagram.", "tiktok.", "twitter.", "x.com", "youtube.", "securityspace.",
    "huaweicloud.", "zhihu.", "indiamart.", "linguee.",
)

NOISE_RE = re.compile(
    r"township|huawei|cdn\s+service|securityspace|known\s+ports|"
    r"lista\s+de\s+puertos|casemine|articles?\s+of\s+association|"
    r"bylaws|communist\s+party|dr\.?\s*web",
    re.I,
)

ADVERSE_HINT_RE = re.compile(
    r"estafa|fraude|corrupci|lavado|soborno|cohecho|ofac|sanci[oó]n|"
    r"investigad|investigaci[oó]n|imputad|acusad|procesad|condenad|"
    r"detenid|arrestad|prisi[oó]n|peculado|efos|denuncia|querella|"
    r"fiscal[ií]a|\bfiscal\b|juzgado|tribunal|judicial|antifraude|"
    r"extorsi[oó]n|fraud|corruption|money\s+laundering|embezzlement|"
    r"bribe|bribery|indicted|convicted|arrested|sanction|blacklist|debar|"
    r"investigation|investigated|accused|prosecuted|criminal|lawsuit|"
    r"litigation|plaintiff|defendant|malpractice|docket|\bcourt\b|"
    r"circuit\s+court|demanda|juicio|expediente|\bvs\.?\b|\bv\.\b",
    re.I,
)


def fold(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn"
    ).lower()


def chunk_keywords(keywords: list[str], size: int = KEYWORD_CHUNK_SIZE) -> list[list[str]]:
    return [keywords[i : i + size] for i in range(0, len(keywords), size)]


def build_name_only_query(sujeto: str) -> str:
    return f'"{sujeto.strip()}"'


def build_keyword_group_query(sujeto: str, keywords: list[str]) -> str:
    ors = " OR ".join(f'"{k}"' for k in keywords)
    return f'"{sujeto.strip()}" AND ({ors})'


def subject_name_tokens(sujeto: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", fold(sujeto)) if len(t) >= 3]


def name_phrase_in(text: str, sujeto: str) -> bool:
    parts = [p for p in re.split(r"[^a-z0-9]+", fold(sujeto)) if len(p) >= 2]
    if not parts:
        return False
    sep = r"\s+"
    inner = sep.join(map(re.escape, parts))
    pat = re.compile(rf"(?<![a-z0-9]){inner}(?![a-z0-9])", re.I)
    return bool(pat.search(fold(text)))


def hit_mentions_subject(sujeto: str, hit: dict[str, Any]) -> bool:
    tokens = subject_name_tokens(sujeto)
    if not tokens:
        return True
    blob = fold(f"{hit.get('title') or ''} {hit.get('description') or ''} {hit.get('link') or ''}")
    matched = sum(1 for t in tokens if t in blob)
    need = min(3, len(tokens))
    if matched >= need:
        return True
    if len(tokens) <= 2:
        return matched == len(tokens)
    return tokens[0] in blob and tokens[-1] in blob


def should_omit_host(link: str) -> bool:
    try:
        host = (urlparse(link).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(frag.rstrip(".") in host for frag in OMIT_HOST_FRAGMENTS)


def is_noise(title: str, snippet: str, link: str) -> bool:
    return bool(NOISE_RE.search(f"{title}\n{snippet}\n{link}"))


def get_serper_api_key() -> str:
    direct = (os.environ.get("SERPER_API_KEY") or "").strip()
    if direct:
        return direct
    arn = (os.environ.get("NUWA_SERPER_SECRET_ARN") or "").strip()
    if not arn:
        return ""
    import boto3

    client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=arn)
    raw = resp.get("SecretString") or ""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return str(data.get("api_key") or data.get("SERPER_API_KEY") or data.get("value") or "").strip()
    except json.JSONDecodeError:
        pass
    return raw.strip()


class SerperUpstreamError(Exception):
    """Raised when google.serper.dev rejects or fails a query."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def _serper_post(api_key: str, q: str, gl: str, hl: str, num: int) -> list[dict[str, Any]]:
    payload = json.dumps({"q": q, "gl": gl, "hl": hl, "num": num}).encode("utf-8")
    req = Request(
        SERPER_URL,
        data=payload,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as ex:
        raise SerperUpstreamError(str(ex), status=int(ex.code or 0) or None) from ex
    except URLError as ex:
        raise SerperUpstreamError(str(ex.reason or ex), status=None) from ex
    organic = data.get("organic") or []
    return organic if isinstance(organic, list) else []


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"\s+", " ", text).strip()


def extract_around_subject(text: str, sujeto: str, radius: int = EXTRACT_RADIUS) -> str:
    folded = fold(text)
    parts = [p for p in re.split(r"[^a-z0-9]+", fold(sujeto)) if len(p) >= 2]
    if not parts:
        return text[: radius * 2]
    phrase = " ".join(parts)
    idx = folded.find(phrase)
    match_len = len(phrase)
    if idx < 0 and len(parts) >= 2:
        loose = re.search(".{0,48}".join(map(re.escape, parts)), folded, re.I)
        if loose:
            idx = loose.start()
            match_len = len(loose.group(0))
    if idx < 0:
        tokens = subject_name_tokens(sujeto)
        if tokens:
            idx = folded.find(tokens[-1])
            match_len = len(tokens[-1])
    if idx < 0:
        return text[: min(len(text), radius * 2)]
    start = max(0, idx - radius)
    end = min(len(text), idx + match_len + radius)
    return text[start:end].strip()


def probe_html(link: str, sujeto: str) -> dict[str, Any]:
    try:
        req = Request(
            link,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; NuwaGoogleSearch/2.0)",
                "Accept": "text/html,application/xhtml+xml",
            },
            method="GET",
        )
        with urlopen(req, timeout=HTML_TIMEOUT_SEC) as resp:
            status = getattr(resp, "status", 200) or 200
            raw = resp.read(HTML_BODY_CAP).decode("utf-8", errors="ignore")
        if status >= 400:
            return {"ok": False, "nameFound": False, "adverse": False, "extract": "", "status": status}
        body = html_to_text(raw)
        name_found = name_phrase_in(body, sujeto) or (
            len(subject_name_tokens(sujeto)) >= 3
            and hit_mentions_subject(sujeto, {"title": "", "description": body, "link": link})
        )
        adverse = bool(ADVERSE_HINT_RE.search(body))
        extract = extract_around_subject(body, sujeto) if name_found else ""
        return {
            "ok": True,
            "nameFound": name_found,
            "adverse": adverse,
            "extract": extract,
            "status": status,
        }
    except Exception as ex:  # noqa: BLE001
        return {
            "ok": False,
            "nameFound": False,
            "adverse": False,
            "extract": "",
            "error": str(ex)[:200],
        }


def merge_snippet(snippet: str, extract: str) -> str:
    base = (snippet or "").strip()
    ex = (extract or "").strip()
    if not ex:
        return base
    if not base:
        return f"[HTML] {ex}"[:1200]
    if fold(ex[:80]) in fold(base):
        return base
    return f"{base}\n[HTML] {ex}"[:1200]


def search_organic(
    api_key: str,
    sujeto: str,
    extra_keywords: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Returns (hits, stats). stats.okJobs==0 + http403 → caller should fail loud."""
    jobs: list[dict[str, Any]] = [
        {"q": build_name_only_query(sujeto), "gl": "mx", "hl": "es", "querySource": "name_only", "label": "name-only-es"},
        {"q": build_name_only_query(sujeto), "gl": "us", "hl": "en", "querySource": "name_only", "label": "name-only-en"},
    ]
    for group in chunk_keywords(ADVERSE_KEYWORDS_ES):
        jobs.append(
            {
                "q": build_keyword_group_query(sujeto, group),
                "gl": "mx",
                "hl": "es",
                "querySource": "keyword",
                "label": "|".join(group),
            }
        )
    for group in chunk_keywords(ADVERSE_KEYWORDS_EN):
        jobs.append(
            {
                "q": build_keyword_group_query(sujeto, group),
                "gl": "us",
                "hl": "en",
                "querySource": "keyword",
                "label": "|".join(group),
            }
        )
    extras = [t.strip() for t in (extra_keywords or []) if t and str(t).strip()]
    for group in chunk_keywords(extras):
        jobs.append(
            {
                "q": build_keyword_group_query(sujeto, group),
                "gl": "mx",
                "hl": "es",
                "querySource": "keyword",
                "label": "industry|" + "|".join(group),
            }
        )

    results: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    ok_jobs = 0
    fail_jobs = 0
    http_403 = 0

    def run_job(
        job: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], SerperUpstreamError | None]:
        try:
            organic = _serper_post(api_key, job["q"], job["gl"], job["hl"], NUM_PER_QUERY)
            return job, organic, None
        except SerperUpstreamError as ex:
            print(f"Serper fail [{job.get('label')}]: {ex}")
            return job, [], ex
        except Exception as ex:  # noqa: BLE001
            print(f"Serper fail [{job.get('label')}]: {ex}")
            return job, [], SerperUpstreamError(str(ex), status=None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=SERPER_CONCURRENCY) as pool:
        for job, organic, err in pool.map(run_job, jobs):
            results.append((job, organic))
            if err is None:
                ok_jobs += 1
            else:
                fail_jobs += 1
                if err.status == 403:
                    http_403 += 1

    # Preserve job order (name-only first)
    by_label = {(j["label"], j["gl"], j["hl"]): org for j, org in results}
    ordered: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for job in jobs:
        key = (job["label"], job["gl"], job["hl"])
        ordered.append((job, by_label.get(key, [])))

    seen: set[str] = set()
    hits: list[dict[str, Any]] = []
    for job, organic in ordered:
        for row in organic:
            link = str(row.get("link") or "").strip()
            if not link or not re.match(r"^https?://", link, re.I):
                continue
            key = link.lower()
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                {
                    "title": str(row.get("title") or "").strip() or link,
                    "link": link,
                    "description": str(row.get("snippet") or "").strip(),
                    "engine": "google",
                    "querySource": job["querySource"],
                }
            )
            if len(hits) >= MAX_SERPER_HITS:
                break
        if len(hits) >= MAX_SERPER_HITS:
            break

    stats = {
        "jobCount": len(jobs),
        "okJobs": ok_jobs,
        "failJobs": fail_jobs,
        "http403": http_403,
    }
    return hits, stats


def select_seeds(sujeto: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered = [
        h
        for h in hits
        if h.get("querySource") == "name_only" or hit_mentions_subject(sujeto, h)
    ]
    chosen = filtered if filtered else list(hits)
    return chosen[:MAX_SERPER_HITS]


def enrich_with_html(sujeto: str, hits: list[dict[str, Any]], *, fetch_html: bool = True) -> list[dict[str, Any]]:
    pre: list[dict[str, Any]] = []
    for h in hits:
        if should_omit_host(h["link"]):
            print(f"SERPER HTML omit site: {h['link']}")
            continue
        if is_noise(h.get("title") or "", h.get("description") or "", h["link"]):
            print(f"SERPER HTML noise drop: {(h.get('title') or '')[:80]}")
            continue
        pre.append(dict(h))

    ready: list[dict[str, Any]] = []
    to_fetch: list[tuple[dict[str, Any], bool]] = []

    for hit in pre:
        blob = f"{hit.get('title') or ''}\n{hit.get('description') or ''}"
        snippet_name = name_phrase_in(blob, sujeto) or hit_mentions_subject(sujeto, hit)
        snippet_adverse = bool(ADVERSE_HINT_RE.search(blob))
        if snippet_name and snippet_adverse:
            hit["keepReason"] = "snippet+adverse"
            hit["htmlFetched"] = False
            ready.append(hit)
            continue
        if not fetch_html:
            hit["keepReason"] = "snippet+name" if snippet_name else "unverified"
            hit["htmlFetched"] = False
            ready.append(hit)
            continue
        needs_html = snippet_name or hit.get("querySource") == "name_only"
        if not needs_html:
            hit["keepReason"] = "skip-html"
            hit["htmlFetched"] = False
            ready.append(hit)
            continue
        to_fetch.append((hit, snippet_name))

    print(f"SERPER HTML parallel: fetch={len(to_fetch)} ready={len(ready)} concurrency={HTML_CONCURRENCY}")

    fetched: list[dict[str, Any]] = []

    def do_one(item: tuple[dict[str, Any], bool]) -> dict[str, Any]:
        hit, snippet_name = item
        probe = probe_html(hit["link"], sujeto)
        if not probe.get("ok"):
            hit["keepReason"] = "snippet+name" if snippet_name else "html-fail"
            hit["htmlFetched"] = True
            return hit
        print(
            f"SERPER HTML: name={probe['nameFound']} adverse={probe['adverse']} | {hit['link'][:120]}"
        )
        if probe["nameFound"] and probe["adverse"]:
            hit["description"] = merge_snippet(hit.get("description") or "", probe.get("extract") or "")
            hit["keepReason"] = "html+adverse"
            hit["htmlFetched"] = True
            return hit
        if probe["nameFound"]:
            hit["description"] = merge_snippet(hit.get("description") or "", probe.get("extract") or "")
            hit["keepReason"] = "html+name"
            hit["htmlFetched"] = True
            return hit
        hit["keepReason"] = "snippet+name" if snippet_name else "html-no-name"
        hit["htmlFetched"] = True
        return hit

    if to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(HTML_CONCURRENCY, len(to_fetch))) as pool:
            fetched = list(pool.map(do_one, to_fetch))

    enriched = ready + fetched

    def rank(h: dict[str, Any]) -> int:
        kr = h.get("keepReason") or ""
        if kr in ("snippet+adverse", "html+adverse"):
            return 0
        if kr in ("html+name", "snippet+name"):
            return 1
        return 2

    enriched.sort(key=rank)
    strong = [h for h in enriched if h.get("keepReason") in ("snippet+adverse", "html+adverse")]
    with_name = [
        h
        for h in enriched
        if h.get("keepReason") in ("snippet+name", "html+name") or h.get("querySource") == "name_only"
    ]
    if strong:
        strong_links = {h["link"] for h in strong}
        return strong + [h for h in with_name if h["link"] not in strong_links]
    return enriched


def run_serper_goog_search(
    search_query: str,
    *,
    extra_keywords: list[str] | None = None,
    fetch_html: bool = True,
) -> dict[str, Any]:
    sujeto = (search_query or "").strip()
    started = time.perf_counter()
    if len(sujeto) < 3:
        return {"success": True, "hits": [], "allHits": [], "meta": {"elapsedMs": 0, "reason": "short_query"}}

    api_key = get_serper_api_key()
    if not api_key:
        return {
            "success": False,
            "code": "SERPER_NOT_CONFIGURED",
            "message": "Falta SERPER_API_KEY o NUWA_SERPER_SECRET_ARN",
            "hits": [],
            "allHits": [],
            "meta": {"elapsedMs": 0},
        }

    all_hits, serper_stats = search_organic(api_key, sujeto, extra_keywords)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # Fail loud: invalid/forbidden API key must not look like "no Google hits".
    if serper_stats.get("okJobs", 0) == 0 and serper_stats.get("http403", 0) > 0:
        print(
            f"SerperGoogSearch FORBIDDEN: sujeto={sujeto[:80]!r} "
            f"jobs={serper_stats.get('jobCount')} http403={serper_stats.get('http403')} ms={elapsed_ms}"
        )
        return {
            "success": False,
            "code": "SERPER_UPSTREAM_FORBIDDEN",
            "message": "Serper API rechazó la key (HTTP 403). Revisar secreto nuwa2/<env>/serper.",
            "hits": [],
            "allHits": [],
            "meta": {"elapsedMs": elapsed_ms, **serper_stats},
        }
    if serper_stats.get("okJobs", 0) == 0 and serper_stats.get("failJobs", 0) > 0:
        print(
            f"SerperGoogSearch UPSTREAM_ERROR: sujeto={sujeto[:80]!r} "
            f"failJobs={serper_stats.get('failJobs')} ms={elapsed_ms}"
        )
        return {
            "success": False,
            "code": "SERPER_UPSTREAM_ERROR",
            "message": "Todas las queries Serper fallaron (red/upstream).",
            "hits": [],
            "allHits": [],
            "meta": {"elapsedMs": elapsed_ms, **serper_stats},
        }

    seeds = select_seeds(sujeto, all_hits)
    enriched = enrich_with_html(sujeto, seeds, fetch_html=fetch_html)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    print(
        f"SerperGoogSearch done: sujeto={sujeto[:80]!r} all={len(all_hits)} "
        f"seeds={len(seeds)} enriched={len(enriched)} ms={elapsed_ms}"
    )
    return {
        "success": True,
        "hits": enriched,
        "allHits": all_hits,
        "meta": {
            "elapsedMs": elapsed_ms,
            "allCount": len(all_hits),
            "seedCount": len(seeds),
            "enrichedCount": len(enriched),
            "fetchHtml": fetch_html,
            **serper_stats,
        },
    }
