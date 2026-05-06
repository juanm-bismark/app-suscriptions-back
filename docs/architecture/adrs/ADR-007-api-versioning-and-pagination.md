# ADR-007 — Versionado de API en URL `/v1/` + cursor pagination + RFC 7807

- **Estado**: Accepted
- **Fecha**: 2026-04-22
- **Decisores**: equipo backend
- **Relacionado**: ADR-004 (modelo de errores)

## Contexto

El modelo canónico de Subscription va a evolucionar. Los proveedores cambian sus contratos. La paginación naive (offset/limit) no funciona cuando los resultados se mezclan en vivo desde N proveedores con orden no estable.

El estado actual del repo expone routers sin prefijo de versión (`/auth`, `/users`, `/companies`). Si se publica el endpoint de SIMs sin versión, romper el contrato será costoso.

## Decisión

### 1. Versionado en URL: prefijo `/v1/`

Todos los endpoints públicos cuelgan de `/v1/`:

```
/v1/auth/login
/v1/auth/signup
/v1/me
/v1/users
/v1/companies
/v1/companies/me/credentials
/v1/companies/me/credentials/{provider}
/v1/companies/me/credentials/{provider}/test
/v1/providers/{provider}/capabilities
/v1/sims
/v1/sims/{iccid}
/v1/sims/{iccid}/usage
/v1/sims/{iccid}/presence
/v1/sims/{iccid}/status                       # PUT
/v1/sims/{iccid}/purge                        # POST
/v1/sims/import                               # POST
```

La implementación actual usa rutas REST explícitas (`/purge`) para acciones no-CRUD.
No se adoptó `:customVerb` en este repo porque el contrato del frontend ya está
basado en rutas `/v1/sims/**` simples.
Las capacidades opcionales como `status_history` se anuncian por
`/v1/providers/{provider}/capabilities`; no forman parte del contrato público de
rutas mientras no exista implementación de router.

Cuando aparezca un cambio incompatible, se introduce `/v2/` en paralelo. `/v1/` se mantiene mínimo 6 meses con deprecation warnings (header `Sunset` y `Deprecation`).

### 2. Paginación cursor-based

Para `GET /v1/sims`:

```
GET /v1/sims?limit=50&cursor=eyJwIjoiVEVMRTIiLCJpIjoiODk..."
```

- `limit`: 1..200, default 50.
- `cursor`: opaco para el cliente, base64 de `{provider, iccid}` para ordenamiento estable cross-proveedor.
- Response:
```json
{
  "items": [...],
  "next_cursor": "eyJ...",
  "has_more": true,
  "partial": false,
  "failed_providers": []
}
```

**Por qué no offset**: el contrato debe soportar tanto cursor nativo del proveedor como página global vía routing map; un OFFSET público acopla al cliente a detalles internos de cada proveedor.

### 3. Errores: RFC 7807 (`application/problem+json`)

Definido en ADR-004. Resumen del shape:

```json
{
  "type": "https://api.example.com/errors/provider.unavailable",
  "title": "Provider unavailable",
  "status": 503,
  "code": "provider.unavailable",
  "detail": "Tele2 timeout after 8000ms",
  "instance": "req_01HXYZ...",
  "provider": "TELE2"
}
```

### 4. OpenAPI

FastAPI genera OpenAPI automáticamente. Hay que:
- Documentar `code` de cada error con `responses=` por endpoint.
- Definir `components.schemas` para `Problem`, `Page<Subscription>`, `Subscription`, etc.
- Servir en `/v1/openapi.json` y `/v1/docs`.

### 5. Headers obligatorios

- Requeridos en cada response: `X-Request-ID` (echo o generado server-side), `X-API-Version: v1`.
- Aceptados en request: `X-Request-ID` (correlación end-to-end con el frontend), `Idempotency-Key` para `POST /v1/sims/{iccid}/purge` (anti doble-clic).

### 6. Idempotencia de mutaciones

Cliente envía `Idempotency-Key: <uuid>` en `POST /v1/sims/{iccid}/purge`. La API guarda en una tabla efímera `idempotency_keys(company_id, key, response, expires_at)` con TTL 24 h y unique `(company_id, key)`. Llamadas repetidas con la misma key y compañía devuelven la respuesta original sin re-invocar al proveedor.

`[REQUIRES INPUT: ¿el frontend ya envía Idempotency-Key o hay que codearlo?]`

## Consecuencias

**Positivas**
- Romper contrato = `/v2/`, no rompe clientes existentes.
- Cursor pagination es estable bajo cambio del catálogo (nuevas SIMs aparecen sin renumerar).
- `code` estable en errores → frontend branchea sin string-matching.
- `Idempotency-Key` previene duplicación de operaciones de control que disparan acciones reales en la red.

**Negativas / mitigaciones**
- El cliente tiene que persistir `next_cursor` en lugar de calcular el siguiente offset → un poco más de fricción → **mitigación**: el cursor es opaco y el cliente no necesita interpretarlo.
- Mantener `/v1/` y `/v2/` en paralelo cuesta → **mitigación**: política explícita de sunset (6 meses).

## Alternativas consideradas

1. **Versionado por header (`Accept: application/vnd.api.v1+json`)**
   - Pros: URL "limpia".
   - Contras: invisible en logs, dificulta debugging y curl manual, ecosistema de tooling más débil.
   - **Rechazada**.

2. **Sin versionado**
   - Pros: simplicidad inicial.
   - Contras: ya identificada como deuda técnica AP-4. Romper contrato = romper clientes.
   - **Rechazada**.

3. **Offset pagination**
   - Pros: trivial.
   - Contras: acopla al cliente al cursor/offset nativo de cada proveedor y se rompe cuando cambia el catálogo. No es opción.
   - **Rechazada**.

4. **Keyset pagination (no-cursor)**
   - Pros: estable.
   - Contras: requiere exponer la columna de ordenamiento al cliente; el cursor opaco es una capa de abstracción más limpia.
   - **Rechazada** a favor de cursor opaco (que internamente es keyset).

## Trade-offs explícitos

| Eje | URL versioning + cursor | Header versioning + offset |
|---|---|---|
| Visibilidad en logs/curl | alta | baja |
| Estabilidad bajo cambio | alta | nula con offset |
| Esfuerzo cliente | medio | bajo |

## Cuándo revisar

- Migración a GraphQL/gRPC (ahí el versionado se mueve al schema/proto).
- Si aparece consumo desde clientes muy primitivos que no soportan headers custom (improbable).
