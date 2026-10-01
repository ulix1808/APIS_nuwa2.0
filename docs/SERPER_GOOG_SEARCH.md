# SerperGoogSearch (`POST /v1/search/serper`)

Lambda dedicada para búsqueda Google adverse-media vía Serper + enriquecimiento HTML.
El BFF la llama desde `grok-search-v2` y usa `hits` como seeds del pass Grok `google_serper_seeded`.

## Deploy (APIs)

1. Desde `APIs/cdk`:
   ```bash
   npx cdk deploy Nuwa2ApiStack-prod --require-approval never
   ```
   Si el secreto `nuwa2/prod/serper` **ya existe** (RETAIN / manual):
   ```bash
   npx cdk deploy Nuwa2ApiStack-prod -c reuseSerperSecret=true
   ```

2. Cargar la API key en Secrets Manager (texto plano o `{"api_key":"..."}`):
   ```bash
   aws secretsmanager put-secret-value \
     --secret-id nuwa2/prod/serper \
     --secret-string "$SERPER_API_KEY" \
     --region us-east-1
   ```

3. Outputs CloudFormation útiles: `SerperGoogSearchFunctionName`, `SerperSecretArn`, `ApiBaseUrl`.

## Contrato

- Auth: `Authorization: Bearer <JWT>`
- Body: `{ clientId, searchQuery, extraKeywords?, fetchHtml? }`
- 200: `{ success, hits, allHits, meta }`
  - `hits` = seeds enriquecidos (para Grok)
  - `allHits` = cap Serper pre-filtro (cobertura / logs)

## Notas

- Lambda **fuera de VPC** (salida a Internet a `google.serper.dev` + HTML GET).
- Timeout 70s, memoria 512 MB.
- BFF fallback local si upstream 404/503/error (`lib/nuwa-serper-client.ts`).
- Forzar local: `SERPER_USE_LOCAL=1` en el BFF.
- Si Serper responde **403** (key inválida), la Lambda devuelve **503** `SERPER_UPSTREAM_FORBIDDEN`
  (no `200` con `hits=[]`). Así el BFF hace fallback local en vez de “sin menciones”.

## Smoke post-deploy (obligatorio)

Tras cargar el secreto, verificar que la key viva responde (no basta el unit test offline):

```bash
KEY=$(aws secretsmanager get-secret-value --secret-id nuwa2/prod/serper \
  --region us-east-1 --query SecretString --output text)
curl -sS -o /tmp/serper-smoke.json -w "%{http_code}\n" \
  -X POST https://google.serper.dev/search \
  -H "X-API-KEY: $KEY" -H "Content-Type: application/json" \
  -d '{"q":"\"Natalia Lucinda Pacheco Chaves\"","gl":"us","hl":"en","num":3}'
# Esperado: http 200 y organic.length > 0
```

Unit tests (`tests/test_serper_search.py`) cubren queries/seeds/HTML y el fail-loud 403
con mock; **no** validan el secreto desplegado en AWS.
