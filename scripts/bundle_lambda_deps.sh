#!/usr/bin/env bash
# Instala dependencias de Lambda (Linux x86_64, Python 3.12) en cdk/lambdas/.
# Ejecutar tras cambiar cdk/lambdas/requirements.txt o en CI antes de cdk deploy.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/cdk/lambdas"
rm -rf psycopg psycopg_binary psycopg_binary.libs typing_extensions.py \
  *.dist-info 2>/dev/null || true
python3 -m pip install --no-cache-dir --upgrade \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  -r requirements.txt \
  -t .
echo "OK — dependencias listas en cdk/lambdas/ (manylinux2014_x86_64, cp312)."
