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
