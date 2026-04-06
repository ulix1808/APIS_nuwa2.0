"""
Cabeceras HTTP comunes para Lambdas detrás de API Gateway (integración proxy).

El preflight CORS lo resuelve API Gateway (`default_cors_preflight_options` en CDK).
En modo proxy, el navegador también necesita `Access-Control-Allow-*` en la respuesta
del método real (GET/POST/…); por eso las Lambdas las devuelven aquí.
"""

from __future__ import annotations

import json
from typing import Any

# Alineado con allow_headers del RestApi en nuwa_api_stack.py
CORS_HEADERS: dict[str, str] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": (
        "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token"
    ),
    "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
}


def json_response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", **CORS_HEADERS}
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }
