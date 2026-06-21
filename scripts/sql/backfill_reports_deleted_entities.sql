-- Limpieza one-shot: reportes active huérfanos de entidades ya deleted.
-- Ejecutar tras deploy de entities_delete + filtro en reports/get.
-- Opcional: AND r.client_id = <tenant> para un solo cliente.

UPDATE public.reports r
SET status = 'deleted', updated_at = now()
WHERE r.status = 'active'
  AND (
    EXISTS (
      SELECT 1 FROM public.entities e
      WHERE e.status = 'deleted'
        AND (e.id = r.entity_id OR e.id = r.parent_entity_id)
    )
    OR (
      r.entity_id IS NULL
      AND TRIM(COALESCE(r.entidad, '')) <> ''
      AND EXISTS (
        SELECT 1 FROM public.entities e
        WHERE e.client_id = r.client_id
          AND e.status = 'deleted'
          AND (
            LOWER(TRIM(COALESCE(e.name, ''))) = LOWER(TRIM(r.entidad))
            OR LOWER(TRIM(COALESCE(e.legal_name, ''))) = LOWER(TRIM(r.entidad))
            OR LOWER(TRIM(COALESCE(e.full_name, ''))) = LOWER(TRIM(r.entidad))
          )
      )
    )
  );
