"""Errores HTTP-style compartidos (PostgREST o PostgreSQL directo)."""

from __future__ import annotations


class SupabaseRestError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body
