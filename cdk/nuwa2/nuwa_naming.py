"""
Convención de nombres físicos en AWS para el stack Nuwa 2.0 API.

Patrón: ``nuwa2-<region>-<environment>-<recurso>``

- Permite listar/borrar por prefijo con CLI (API Keys, Lambdas, etc.).
- Las etiquetas ``nuwa:*`` (aplicadas en el stack) refuerzan el filtrado vía Resource Groups / Tag Editor.

Región por defecto ``us-east-1`` alineada al despliegue previsto; si despliegas en otra región,
pasa ``aws_region`` explícitamente para que los nombres coincidan con ``Stack.env.region``.
"""

from __future__ import annotations


def nuwa_name_prefix(*, environment_name: str, aws_region: str = "us-east-1") -> str:
    """Prefijo común sin guión final, p.ej. ``nuwa2-us-east-1-prod``."""
    return f"nuwa2-{aws_region}-{environment_name}"


# Claves de etiquetas (usar en Resource Groups: nuwa:project = nuwa2)
TAG_PROJECT = "nuwa:project"
TAG_ENVIRONMENT = "nuwa:environment"
TAG_NAME_PREFIX = "nuwa:name-prefix"
TAG_MANAGED_BY = "nuwa:managed-by"

TAG_VALUE_PROJECT = "nuwa2"
TAG_VALUE_MANAGED_BY = "cdk-nuwa2-api"
