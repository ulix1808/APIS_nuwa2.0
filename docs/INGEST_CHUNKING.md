# Ingest y chunking para Nuwa 2.0 (searchEngine2.0 / Supabase)

Este documento es para quien implemente el **partidor de archivos (chunking)** desde **Vercel** (u otro runtime serverless) antes de enviar los chunks al API que persiste en Supabase (`risk_entity_chunks`).

La búsqueda en base de datos usa **`word_similarity`** y **full-text (`tsvector`)** solo sobre **`chunk_text`**. No existe columna `aliases`: el nombre que el usuario escribe al buscar **no** se guarda en ingest; lo que importa es que **cada chunk** contenga el texto suficiente y legible para que ese nombre (con typos leves) aparezca **dentro** del chunk.

---

## 1. Objetivo del chunk

Cada fila en `risk_entity_chunks` representa **un trozo indexable** de una fuente (p. ej. SAT), con:

| Campo (DB)      | Rol |
|-----------------|-----|
| `source_id`     | `bigint`: id numérico del catálogo de fuentes (mismo que al borrar la fuente). |
| `client_id`     | Tenant. |
| `risk_level`    | `1` = low, `2` = medium, `3` = high (homologado con otras APIs). |
| `entity_type`   | P. ej. `company`, `person` (según negocio / extracción). |
| `chunk_text`    | **Texto plano buscable** (ver reglas abajo). |
| `visibility`    | `public` \| `private` según si la fuente es visible para todos los tenants o solo el creador. |

**Regla de oro:** un chunk debe ser **coherente semánticamente** (típicamente **una fila de CSV**, **un bloque de texto**, o **una página / sección de PDF**) y el **`chunk_text`** debe incluir **etiquetas o separadores** que hagan obvio qué es nombre, RFC, domicilio, etc., para que trigramas y tokens encuentren “EVCON GROUP” aunque venga mezclado con otros campos.

---

## 2. Reglas generales para un buen `chunk_text`

1. **Texto plano UTF-8.** Sin depender de que el buscador “entienda” JSON; si usas JSON como transporte, el valor que indexas debe ser **string** con contenido legible.
2. **Nombres explícitos.** Prefija campos con etiquetas en el mismo idioma del dato, p. ej. `Razon social: …`, `Nombre: …`, `RFC: …`. Así una fila CSV partida en columnas queda **una sola cadena** con sentido para búsqueda y para el **snippet** del reporte.
3. **Orden estable.** Misma convención en todos los chunks de la misma fuente (siempre `RFC` antes que `Nombre`, etc.) para depuración y QA.
4. **Evita solo separadores frágiles.** `|` o `;` están bien si son consistentes; mezcla `tab` + espacios sin etiquetas empeora la legibilidad para humanos y para `ts_headline` cuando hay match exacto.
5. **Tamaño razonable.** Ni micro-chunks (solo un apellido) ni un chunk de 500 KB. Orientación práctica: **~500–8 000 caracteres** por chunk salvo PDFs muy densos; si una fila CSV es enorme, divide por **campos lógicos** (identidad en un chunk, domicilio en otro) **solo si** el negocio acepta dos hits para el mismo registro.
6. **Normalización ligera en ingest (opcional):** colapsar espacios, trim, NFC Unicode. **No** borrar acentos en DB (ya indexas `simple`); si quieres doble vía con sin acentos, hazlo en el string que guardas, no en una segunda columna obligatoria.
7. **PDFs:** el texto extraído suele perder tablas; donde sea crítico, post-procesar con layout (ver §5) o extracción asistida (Grok) **antes** de formar `chunk_text`.

---

## 3. Fuentes CSV

### Estrategia recomendada

- **Un chunk = una fila de datos** (excluyendo header si no aporta entidades).
- Construir `chunk_text` concatenando **cabecera de columna + valor** por cada celda relevante.

### Ejemplo de archivo

```csv
rfc,nombre_razon,regimen,estatus
ABC123456789,EVCON GROUP SA DE CV,601,activo
DEF987654321,COMERCIALIZADORA DEL NORTE SC,601,baja
```

### Ejemplo de `chunk_text` (fila 1)

```text
Fuente: SAT_listado_ejemplo | RFC: ABC123456789 | Razon social: EVCON GROUP SA DE CV | Regimen: 601 | Estatus: activo
```

### Ejemplo de `chunk_text` (fila 2)

```text
Fuente: SAT_listado_ejemplo | RFC: DEF987654321 | Razon social: COMERCIALIZADORA DEL NORTE SC | Regimen: 601 | Estatus: baja
```

### Notas

- Si el CSV **no** tiene encabezados, define un **mapeo fijo** por posición y **inventa etiquetas** estables (`Campo1: …`) o rechaza el archivo hasta tener esquema.
- CSV con **múltiples filas por entidad** (histórico): o un chunk por fila (más hits) o agrupa por clave (RFC) en un solo chunk con varias líneas numeradas; la segunda opción mejora contexto pero aumenta tamaño.
- **Streaming:** en Vercel, lee el CSV por líneas (`readline`, `csv-parse` stream) para no cargar el archivo entero en memoria si es grande.

### Payload lógico hacia tu API (ejemplo)

```json
{
  "sourceId": 42,
  "clientId": 1234,
  "riskLevel": 2,
  "visibility": "private",
  "entityType": "company",
  "chunks": [
    {
      "order": 0,
      "chunkText": "Fuente: SAT_listado_ejemplo | RFC: ABC123456789 | Razon social: EVCON GROUP SA DE CV | Regimen: 601 | Estatus: activo"
    }
  ]
}
```

---

## 4. Fuentes TXT

### Estrategia recomendada

- Si el archivo es **lista de nombres** (una línea = una entidad): **un chunk = una línea** (o bloque de líneas si una entidad ocupa varias líneas con indentación).
- Si es **prosa** (informe): **un chunk = párrafo** o **ventana de N líneas** con solape (overlap) del 10–20% entre chunks contiguos para no cortar nombres en el borde.

### Ejemplo A — lista simple

Archivo:

```text
EVCON GROUP SA DE CV
COMERCIALIZADORA DEL NORTE SC
JUAN PEREZ LOPEZ
```

Chunk por línea (`chunk_text`):

```text
Entidad: EVCON GROUP SA DE CV
```

```text
Entidad: COMERCIALIZADORA DEL NORTE SC
```

```text
Persona: JUAN PEREZ LOPEZ
```

Aquí `entityType` podría venir de heurística (`person` vs `company`) o fijo por fuente.

### Ejemplo B — texto corrido

Archivo (fragmento):

```text
La empresa EVCON GROUP SA DE CV figura en el apartado de contribuyentes
con RFC ABC123456789. En el mismo listado aparece COMERCIALIZADORA DEL NORTE SC.
```

Opción párrafo (un chunk):

```text
Fuente: informe_2024.txt | Parrafo: 1 | La empresa EVCON GROUP SA DE CV figura en el apartado de contribuyentes con RFC ABC123456789. En el mismo listado aparece COMERCIALIZADORA DEL NORTE SC.
```

Si partes en dos chunks por tamaño, usa **solape** para que “EVCON GROUP” no quede solo al final de un chunk sin contexto “RFC …” en el siguiente.

---

## 5. Fuentes PDF

### Estrategia recomendada

1. **Extracción de texto:** `pdf-parse`, `pdfjs-dist`, o servicio externo si el PDF es escaneado (OCR).
2. **Chunking:** por **página** o por **bloque de líneas** con límite de caracteres; en tablas, preferir librerías que preserven **orden de lectura** (filas).
3. Prefijo en cada chunk: `Fuente: <nombre> | Pagina: N | …` para trazabilidad en reportes.

### Ejemplo conceptual (tabla en PDF convertida a texto)

```text
Fuente: SAT_pdf_listado | Pagina: 3 | RFC: ABC123456789 | Razon social: EVCON GROUP SA DE CV | Regimen: 601 | Estatus: activo
```

### Limitaciones

- PDFs escaneados sin OCR: el `chunk_text` puede ser basura; **no** indexar hasta tener texto fiable.
- Columnas visualmente separadas pueden **concatenarse mal**; revisa una muestra manual o usa extracción con layout / LLM solo en ingest para **reconstruir filas** antes de armar `chunk_text` tipo CSV lógico.

---

## 6. Vercel: limitaciones prácticas

| Tema | Recomendación |
|------|----------------|
| **Timeout** | Funciones serverless tienen límite de duración (p. ej. 10 s Hobby, hasta 60 s Pro). Archivos grandes: **subir a S3 / Supabase Storage** y procesar en **cola** (Inngest, QStash, Step Functions) o **Edge + desacople**. |
| **Memoria** | No leas PDFs de 200 MB enteros en un solo request; stream o job asíncrono. |
| **Tamaño del body** | Si el API recibe muchos chunks, usa **paginación** o **carga por lotes** (batch) con `order` monotónico. |
| **Secretos** | API keys y `service_role` de Supabase solo en env del servidor, nunca en el cliente. |

Flujo típico robusto:

1. Cliente sube archivo → Vercel guarda referencia (Storage) y encola job.
2. Worker (misma app o Lambda) descarga, chunking, POST batches al API de ingest.
3. Respuesta al usuario: `jobId` + estado.

---

## 7. Relación con la búsqueda (por qué importa el formato)

- **`word_similarity('evcom group', chunk_text)`** encuentra la **mejor subcadena** del chunk parecida a la consulta. Si el nombre real está en el chunk como `Razon social: EVCON GROUP SA DE CV`, un typo en “evcom” sigue siendo cercano a “EVCON”.
- **`fts`** ayuda cuando el usuario escribe tokens correctos; con typos fuertes manda **`word_similarity`**.
- El campo **`snippet`** en la RPC usa **`ts_headline`** cuando hay match FTS; si el match es solo por similitud (typo), el SQL devuelve un **preview** del inicio del chunk — por eso conviene que **nombre + identificadores** aparezcan **pronto** en el string o en las primeras ~500 caracteres cuando sea posible.

---

## 8. Checklist rápido antes de dar por bueno un ingest

- [ ] Cada chunk tiene prefijo `Fuente:` / `Pagina:` / `RFC:` (o equivalente) según aplique.
- [ ] Una fila CSV no queda como valores sueltos sin etiquetas.
- [ ] Se probó una búsqueda manual con nombre **correcto** y con **typo leve** contra 2–3 chunks de prueba en Supabase (`search_risk_entities`).
- [ ] `source_id`, `risk_level`, `entity_type` y `visibility` son coherentes con el catálogo de fuentes.

---

## 9. Siguiente paso de producto

Definir el contrato HTTP definitivo del **API de ingest** (batch size, idempotencia por `sourceId + order`, reemplazo total vs append) y dónde vive el **catálogo** (esta base u otro servicio); eso cierra el flujo extremo a extremo con las preguntas de arquitectura pendientes.
