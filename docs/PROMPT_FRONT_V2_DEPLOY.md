# Prompt — Frontend Nuwa 2.0 en `app.nuwa.space/v2`

Copia este documento completo en el agente de Cursor del **repo del frontend** (Next.js) y ejecútalo como tarea de implementación + deploy.

---

## Objetivo

Configurar el frontend Nuwa 2.0 para producción en el subpath **`/v2`**, accesible en:

- **Producción:** `https://app.nuwa.space/v2`
- **API backend:** `https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod`

La infraestructura AWS ya está lista; falta ajustar el código del front, variables de entorno, Docker y redeploy del contenedor en la EC2.

---

## Infraestructura ya configurada (no modificar salvo health check)

| Recurso | Valor |
|---------|-------|
| Dominio | `app.nuwa.space` → ALB `nuwa-app` |
| EC2 | `Nuwa_OptimusPrime` — IP pública `3.92.3.96` |
| Contenedor | Next.js en puerto **3001** |
| Target group ALB | `nuwa-front-v2` → instancia `i-068d242c31169c509:3001` |
| Regla ALB | Path `/v2*` → target group `nuwa-front-v2` (listeners :443 y :80) |
| Health check ALB | Temporal: path `/`, matcher `200-404` (cambiar a `/v2` cuando basePath funcione) |
| CORS S3 documentos | Orígenes: `https://app.nuwa.space`, `http://app.nuwa.space`, localhost |
| Bucket documentos | `nuwa2-us-east-1-prod-client-documents` |

**No** apuntes el front directo a `http://3.92.3.96:3001` en el navegador ni en variables públicas. Usa siempre `https://app.nuwa.space/v2`.

---

## 1. Next.js — `basePath: '/v2'` (obligatorio)

El ALB reenvía las peticiones **con** el prefijo `/v2` al contenedor. Next.js debe declarar ese basePath.

### `next.config.js` / `next.config.mts`

```js
/** @type {import('next').NextConfig} */
const nextConfig = {
  basePath: '/v2',
  // assetPrefix: '/v2',  // solo si _next/static falla tras probar; basePath suele bastar
};

module.exports = nextConfig;
```

### Implicaciones App Router

- `<Link href="/dashboard">` → Next prefija automáticamente → `/v2/dashboard`
- `useRouter().push('/login')` → respeta basePath
- **Evitar** URLs absolutas hardcodeadas sin basePath: `href="/dashboard"` en `<a>` nativo fallará
- **Evitar** `fetch('/api/...')` sin considerar que las rutas BFF viven bajo `/v2/api/...` desde el browser (Next API routes siguen siendo `/api/*` internamente; el browser las llama como `/v2/api/*`)

### Redirect raíz (opcional)

Si alguien entra a `https://app.nuwa.space/` (sin `/v2`), el ALB sigue yendo al legacy :8080. No es responsabilidad del contenedor :3001 salvo que quieras un middleware local en `/` → `/v2` para pruebas directas al puerto 3001.

---

## 2. Variables de entorno

### Producción (EC2 / docker-compose / `.env.production`)

```env
PORT=3001
HOSTNAME=0.0.0.0
NODE_ENV=production

# API Nuwa 2.0 — API Gateway prod
NUWA_PROD_API_BASE=https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod
NEXT_PUBLIC_NUWA_API_BASE=https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod

# URL pública del front (redirects, metadata, cookies si aplica)
NEXT_PUBLIC_APP_URL=https://app.nuwa.space/v2
```

### Desarrollo local

```env
PORT=3001
NUWA_PROD_API_BASE=https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod
NEXT_PUBLIC_NUWA_API_BASE=https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod
NEXT_PUBLIC_APP_URL=http://localhost:3001/v2
```

En local, arranca con `basePath: '/v2'` y abre `http://localhost:3001/v2`.

---

## 3. Autenticación y proxy BFF

Todas las rutas `/api/*` del front deben hacer proxy al backend Nuwa:

- Header: `Authorization: Bearer {accessToken}`
- Body: incluir **`clientId`** y **`userId` numéricos** (claims JWT `cid` y `sub`)
- Sesión UI: `localStorage.nuwa_session` con `numericClientId`, `numericUserId`, `accessToken`

### Login

```http
POST {NUWA_PROD_API_BASE}/v1/auth/login
Content-Type: application/json

{ "email": "...", "password": "...", "clientId": 1 }
```

Respuesta: `accessToken` → usar en el resto de llamadas.

---

## 4. Rutas BFF → API Nuwa (referencia rápida)

| Ruta Next (BFF) | Upstream Nuwa |
|-----------------|---------------|
| `POST /api/entities/match` | `POST /v1/entities/match` |
| `POST /api/entities` | `POST /v1/entities/create` |
| `GET /api/entities` | `POST /v1/entities/list` |
| `GET /api/entities/[id]` | `POST /v1/entities/get` |
| `PATCH /api/entities/[id]` | `POST /v1/entities/update` |
| `DELETE /api/entities/[id]` | `POST /v1/entities/delete` |
| stats widgets | `POST /v1/entities/stats` |
| `POST /api/monitoring` | `POST /v1/entities/monitoring/upsert` |
| `GET /api/monitoring` | `POST /v1/entities/monitoring/list` |
| `POST /api/screening/save-report` | `POST /v1/reports/save` (+ `entityId` en body) |

Detalle completo de entidades, catálogos Rol/Relación y campos PF/PM: ver en repo APIs `docs/PROMPT_INTEGRACION_FRONT_ENTIDADES.md`.

---

## 5. Módulo documentos (backend ya desplegado)

Flujo desde el front:

```
1. POST /v1/clients/storage/init     (una vez por clientId)
2. POST /v1/documents/presign
3. PUT  {uploadUrl}                  (browser → S3 directo)
4. POST /v1/documents/upload-complete
5. POST /v1/documents/finalize       (extractedJson desde Grok en BFF)
6. POST /v1/documents/list | get | update | delete | download-url
```

CORS S3 permite origen `https://app.nuwa.space`. Si el upload directo a S3 falla por CSP, añade el endpoint S3 en `connect-src` (ver §6).

Entidades auto-creadas con `category=document_mention` no aparecen en listados salvo `includeDocumentMentions: true`.

---

## 6. Content-Security-Policy

El deploy actual devuelve CSP restrictivo. Actualiza headers en `next.config` (o middleware) para permitir llamadas al API Gateway:

```js
// Ejemplo en next.config.js → headers()
{
  key: 'Content-Security-Policy',
  value: [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: blob: https:",
    "connect-src 'self' https://yswipjmkgg.execute-api.us-east-1.amazonaws.com https://*.s3.amazonaws.com https://*.s3.us-east-1.amazonaws.com https://api.searchenginepost.com",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join('; '),
}
```

Ajusta dominios adicionales que use el front (analytics, Grok, etc.).

---

## 7. Docker / despliegue en EC2

### Dockerfile / compose

- Exponer puerto **3001**: `- "3001:3001"`
- Escuchar en todas las interfaces: `HOSTNAME=0.0.0.0`
- Comando: `next start -p 3001` (o `node server.js` con `PORT=3001`)

### Ejemplo docker-compose (fragmento)

```yaml
services:
  nuwa-front:
    build: .
    ports:
      - "3001:3001"
    environment:
      PORT: 3001
      HOSTNAME: 0.0.0.0
      NODE_ENV: production
      NUWA_PROD_API_BASE: https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod
      NEXT_PUBLIC_NUWA_API_BASE: https://yswipjmkgg.execute-api.us-east-1.amazonaws.com/prod
      NEXT_PUBLIC_APP_URL: https://app.nuwa.space/v2
    restart: unless-stopped
```

### Tras deploy en la EC2

```bash
# En la instancia Nuwa_OptimusPrime
docker compose pull && docker compose up -d --build
curl -I http://127.0.0.1:3001/v2
curl -I http://127.0.0.1:3001/v2/_next/static/   # algún asset real
```

---

## 8. Checklist de verificación

- [ ] `curl -I https://app.nuwa.space/v2` → **HTTP 200** (no 404)
- [ ] `https://app.nuwa.space/v2/_next/static/...` carga JS/CSS
- [ ] Login funciona contra `/v1/auth/login`
- [ ] Llamadas autenticadas con Bearer a `/v1/*` responden OK
- [ ] Links internos navegan bajo `/v2/...` sin salir del subpath
- [ ] Upload de documentos a S3 (presigned) funciona desde el browser
- [ ] Target group ALB `nuwa-front-v2` en estado **healthy**

### Endurecer health check (post-deploy)

Cuando `/v2` responda 200, pedir al equipo de infra/backend:

```bash
aws elbv2 modify-target-group \
  --target-group-arn arn:aws:elasticloadbalancing:us-east-1:100906894518:targetgroup/nuwa-front-v2/31a8072143d337c0 \
  --health-check-path /v2 \
  --matcher HttpCode=200-399
```

---

## 9. Qué NO hacer

- No quitar `basePath: '/v2'` si el tráfico entra por `app.nuwa.space/v2`
- No cambiar DNS de `app.nuwa.space` (ya apunta al ALB correcto)
- No usar `http://3.92.3.96:3001` como URL pública del producto
- No commitear contraseñas ni tokens en `.env` del repo

---

## 10. Instrucción para el agente de Cursor

Implementa todos los cambios anteriores en este repositorio frontend:

1. Añade `basePath: '/v2'` en `next.config`.
2. Configura variables de entorno de prod y local según §2.
3. Revisa que todas las rutas `/api/*` proxy a `{NUWA_PROD_API_BASE}/v1/...` con Bearer y `clientId`/`userId` numéricos.
4. Actualiza CSP en §6 para incluir el API Gateway (y S3 si hay documentos).
5. Ajusta Dockerfile/compose para puerto 3001 y `HOSTNAME=0.0.0.0`.
6. Busca links hardcodeados sin basePath y corrígelos.
7. Ejecuta build local (`npm run build` / `pnpm build`) y confirma que no hay errores.
8. Documenta en README del front los pasos de deploy en EC2.

Si existe integración de entidades incompleta, alinea con la spec del repo APIs en `docs/PROMPT_INTEGRACION_FRONT_ENTIDADES.md` (copiar ese archivo al contexto si hace falta).

Al terminar, lista los archivos modificados y los comandos exactos para rebuild + redeploy del contenedor en la EC2.
