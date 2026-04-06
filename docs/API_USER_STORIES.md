# Historias de usuario — APIs Nuwa 2.0

Formato: **como** [rol] **quiero** … **para** …

## Login y JWT

- **Como** usuario de la app **quiero** iniciar sesión con email/contraseña **para** obtener un `accessToken` y llamar al API con `Authorization: Bearer`.
- **Como** integrador M2M **quiero** seguir teniendo API Keys por tenant en API Gateway **para** cuotas o scripts; la app web usa JWT (ver `docs/API_AND_ARCHITECTURE.md`).

## Fuentes (`/v1/sources/*`)

- **Como** administrador Nuwa (`clientId=1`, `userId=1`) **quiero** crear fuentes siempre públicas **para** que todos los clientes puedan buscar en ellas.
- **Como** usuario de una empresa **quiero** crear una fuente privada o pública **para** controlar si solo mi `clientId` la ve o todos.

## Chunks (`/v1/chunks/ingest`)

- **Como** sistema de ingest (Vercel / pipeline) **quiero** reemplazar todos los chunks de una fuente **para** actualizar listados grandes con una ventana breve sin datos aceptable.

## Búsqueda (`/v1/search`)

- **Como** analista **quiero** buscar por nombre y/o RFC **para** obtener snippets y metadatos de fuente antes de generar un reporte con Grok.

## Reportes (`/v1/reports/*`)

- **Como** usuario **quiero** guardar el JSON del reporte generado **para** consultarlo después por `folio`.
- **Como** usuario **quiero** listar solo mis reportes **para** no ver datos de otros.
- **Como** admin de compañía **quiero** listar reportes de todos los usuarios `user` de mi `clientId` **para** supervisar el trabajo del equipo.
- **Como** super admin **quiero** filtrar por cualquier `clientId` **para** operar la plataforma.

## Administración (`/v1/admin/*`)

- **Como** super admin **quiero** crear compañías y usuarios **para** incorporar nuevos clientes (JWT + body alineados; rol verificado en BD).
- **Como** admin de compañía **quiero** crear usuarios con rol `user` **para** que operen solo dentro de mi tenant (sin crear compañías ni tocar otros tenants).
- **Como** usuario con rol `user` **no** debo poder llamar a admin **para** que el backend responda 403.
- **Como** quien usa admin **quiero** enviar `clientId`/`userId` en el body iguales al token **para** que la Lambda acepte la petición.

---

Las rutas, JWT (`BearerAuth`) y RBAC están en `openapi/openapi.yaml`. Diagramas y amenazas: `docs/API_AND_ARCHITECTURE.md` § 1.
