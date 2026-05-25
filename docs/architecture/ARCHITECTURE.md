# Subscriptions API — Architecture

## Executive Summary

Esta API centraliza, bajo un modelo de dominio único, la consulta y las operaciones de control sobre las ~134 612 SIMs M2M/IoT de la organización distribuidas entre tres proveedores externos (Kite, Tele2 y Moabits). Opera como **proxy en tiempo real**: no mantiene una copia local del catálogo — la fuente de verdad es siempre el proveedor. El resultado es una API que el frontend interno puede usar sin conocer qué proveedor vive detrás, que aísla fallas de un proveedor de los demás, y que permite agregar un cuarto proveedor sin modificar el dominio.

## Status

| | |
|---|---|
| **Fecha** | 2026-05-25 |
| **Versión** | 1.1 |
| **Estado** | Accepted — arquitectura vigente alineada con ADR-012 fases A/B/C |
| **Autores** | Equipo backend + solution architect |
| **Depth del ejercicio** | comprehensive (Phases 1–8) |
| **Últimas actualizaciones** | 2026-05-25: ADR-012 aceptado parcialmente implementado: Redis + Arq worker, `sync_jobs`, cron nocturno, `/v1/sync/trigger`, `/v1/sync/status`, `/v1/jobs/{job_id}` y `POST /v1/sims/details`. Pendiente: `POST /v1/sims/export`. Moabits usa `company_provider_mappings` para el company code operativo y `moabits_source_companies` como cache de discovery (ADR-010). 2026-05-07: Moabits v2 enrichment para `GET /v1/sims?provider=moabits` se intenta por defecto (`MOABITS_V2_ENRICHMENT_ENABLED=true`) después de descubrir ICCIDs con v1 `simList`; `false` conserva salida legacy v1-only (ADR-011). 2026-05-06: `SubscriptionOut` agrega `detail_level` y `normalized`; Tele2 listing enriquece hasta 5 SIMs por página con `Get Device Details`. |

---

## Context

### Business context
- La organización gestiona líneas M2M/IoT para sus clientes empresariales.
- Hoy cada proveedor se consulta por herramientas distintas — UX fragmentada, imposibilidad de ver el catálogo completo en un solo lugar.
- Objetivo: una sola API (y un solo frontend interno) para las tres plataformas.

Listing behaviour:
- Provider-scoped listing: `GET /v1/sims?provider=<name>` resolves
  `company_provider_credentials` and uses the provider's native pagination
  when the adapter implements `SearchableProvider`.
- Global listing (sin `provider`): uses known routing first and can bootstrap a
  small provider page when the routing map is empty or incomplete. ADR-012 adds
  periodic/manual routing sync so global pages are not dependent only on lazy
  discovery.
- Batch details: `POST /v1/sims/details` resolves each ICCID through routing,
  prefix fallback and lazy discovery, then fetches current details live from the
  owning provider. No SIM state is cached locally.

Rationale: `sim_routing_map` is the routing index that decides which
credentials/provider to use for each `iccid` and prevents repeated
cross-provider fan-out. Ver `migrations/001_sim_routing_map.sql`,
`migrations/002_company_provider_credentials.sql` y `migrations/008_sync_jobs.sql`.

### Technical context
- Stack existente: FastAPI (async) + SQLAlchemy 2.0 + asyncpg + PyJWT + bcrypt + httpx.
- Multi-tenant por `Company` con RBAC (`admin`/`manager`/`member`/`public`).
- Desplegado en Docker. Postgres managed (tipo Supabase o equivalente).

### Constraints
- **Modo proxy preservado**: no hay almacén canónico de estado de SIM. El detalle (`status`, `msisdn`, `usage`, `presence`, plan, etc.) sigue viviendo sólo en el proveedor. ADR-012 agrega sync periódico únicamente para `sim_routing_map`.
- **Escala**: 15–20 usuarios concurrentes, 134 612 SIMs totales, routing sync nocturno y batch details hasta 200 ICCIDs.
- **Equipo**: `[ASSUMPTION: team_size < 5]` — descarta microservicios.

---

## Architecture Decisions

Detalle completo en [adrs/](adrs/).

| # | Decisión | Link | Estado |
|---|---|---|---|
| ADR-001 | Modular Monolith (paquetes por bounded context, single deployable) | [ADR-001](adrs/ADR-001-modular-monolith.md) | Accepted |
| ADR-002 | Proxy en tiempo real — sin almacén canónico; sólo `SIM Routing Map` | [ADR-002](adrs/ADR-002-real-time-proxy-no-canonical-store.md) | Accepted |
| ADR-003 | Anti-Corruption Layer + `SubscriptionProvider` Protocol + adapter por proveedor + Registry | [ADR-003](adrs/ADR-003-acl-provider-adapter.md) | Accepted |
| ADR-004 | Modelo canónico de errores → RFC 7807 Problem Details | [ADR-004](adrs/ADR-004-error-model.md) | Accepted |
| ADR-005 | Resiliencia: timeout + retry idempotente + circuit breaker + cache TTL≤5s + bulkhead | [ADR-005](adrs/ADR-005-resilience-and-cache.md) | Accepted |
| ADR-006 | Credenciales por tenant en tabla cifrada con Fernet, no en JSONB de settings | [ADR-006](adrs/ADR-006-encrypted-credentials-table.md) | Accepted |
| ADR-007 | Versionado URL `/v1/` + cursor pagination + `Idempotency-Key` obligatoria en mutaciones | [ADR-007](adrs/ADR-007-api-versioning-and-pagination.md) | Accepted |
| ADR-008 | JWT existente reutilizado + RBAC + scope por `Company` + `audit_log` | [ADR-008](adrs/ADR-008-auth-rbac-audit.md) | Accepted |
| ADR-009 | Pirámide de tests + golden files de mappers + contract tests + FakeProvider | [ADR-009](adrs/ADR-009-testing-strategy.md) | Accepted |
| ADR-010 | Moabits: bootstrap explícito del company code (sin auto-scope por nombre) | [ADR-010](adrs/ADR-010-moabits-explicit-company-codes-bootstrap.md) | Accepted |
| ADR-011 | Moabits: enrichment v2 por defecto para listado provider-scoped | [ADR-011](adrs/ADR-011-moabits-v2-list-enrichment.md) | Accepted |
| ADR-012 | Routing sync con Arq + Redis, batch details y jobs async | [ADR-012](adrs/ADR-012-routing-sync-and-async-jobs.md) | Accepted — fases A/B/C implementadas |

---

## Domain Model (resumen)

Detalle en [domain-model.md](domain-model.md) y [context-map.mermaid](context-map.mermaid).

**Bounded contexts**:
1. **Identity & Access** *(existente)* — `User`, `Profile`, `RefreshToken`.
2. **Tenancy** *(extendido)* — `Company`, `CompanySettings`, `CompanyProviderCredentials` (nuevo).
3. **Subscription Aggregation** *(nuevo)* — aggregate `Subscription`, `SIM Routing Map`. Sin persistir estado del SIM.
4. **Provider Integration** *(nuevo, técnico)* — ACL con un adapter por proveedor.

**Aggregate root**: `Subscription` identificado por `iccid`, con `status` como valor crudo de proveedor, value objects `ConnectivityPresence`, `UsageSnapshot`, `UsageMetric`, `CommercialPlan`, `ConsumptionLimit`, y el historial `StatusChange`.

**Lenguaje ubicuo**: vocabulario de proveedor (`icc`, `lifeCycleStatus`, `simStatus`, `Edit Device Details`, …) aparece dentro de Provider Adapters y se expone como `status` cuando representa el estado administrativo del proveedor. El dominio y los routers usan únicamente `iccid`, `status` y `ControlOperation`.

**Decisiones semánticas críticas**:
- `ControlOperation` — Operación canónica `purge` mapeada por adapters a las APIs proveedoras (p.ej. Kite `networkReset`, Tele2 `Edit Device {status: PURGED}`, Moabits `PUT /api/sim/purge/`). Operaciones no soportadas por el provider de la SIM devuelven `409 UnsupportedOperation`.
- El contrato expuesto al frontend separa:
  - campos top-level (`iccid`, `msisdn`, `imsi`, `status`, `provider`, fechas);
  - `normalized` (bloques homogéneos para UI: `identity`, `status`, `plan`, `customer`, `network`, `hardware`, `services`, `limits`, `dates`, `custom_fields`);
  - `provider_fields` (bloque dinámico de atributos del proveedor para vistas avanzadas);
  - `provider_metrics`/`usage_metrics` (consumo normalizado y métricas nativas).
- `detail_level` indica si una fila de listado es `summary` o `detail`. En Tele2, las filas no enriquecidas por `Get Device Details` se devuelven como `summary`; sus campos `null` no significan ausencia real del dato.

---

## System Design

### C4 Level 1 — System Context
Ver [c4-context.mermaid](c4-context.mermaid).

Un único sistema `Subscriptions API` consumido por usuarios internos (autenticados con JWT), conectado a Postgres y a los tres proveedores externos.

### C4 Level 2 — Containers
Ver [c4-container.mermaid](c4-container.mermaid).

Dentro del sistema: `FastAPI App` (routers + middleware), `Subscription Aggregation Module` (dominio + services), `Provider Adapters` (ACL), `Identity & Tenancy Module`. Todo deploy en un proceso; se comunican **in-process**.

### C4 Level 3 — Components
Ver [c4-component.mermaid](c4-component.mermaid).

Dentro de Subscription Aggregation: los use cases `SubscriptionFetcher`, `SubscriptionSearchService` y `SubscriptionOperationService` están implementados hoy como helpers/rutas en `app/subscriptions/routers/sims.py`; `ProviderRegistry` y `sim_routing_map` sí existen como componentes explícitos. Dentro de Provider Adapters: `SubscriptionProvider` Protocol + `KiteAdapter` / `Tele2Adapter` / `MoabitsAdapter`.

### Patrones (resumen por dimensión)

| Dimensión | Patrón |
|---|---|
| Topología | Modular Monolith (ADR-001) |
| Comunicación | HTTP sync por adapter; sin fan-out cross-provider por defecto (ADR-005) |
| Propiedad de datos | Proxy puro, cero espejo (ADR-002) |
| API style | REST + `/v1/` + cursor pagination + RFC 7807 (ADR-007) |
| AuthN/AuthZ | JWT propio + RBAC + scope por Company (ADR-008) |
| Testing | Pirámide + golden files + contract tests (ADR-009) |
| ACL | Adapter por proveedor + Protocol + Registry (ADR-003) |

---

## Non-Functional Requirements (resumen)

Tabla completa en [nfr-analysis.md](nfr-analysis.md).

| Categoría | Ejemplos con número |
|---|---|
| Performance | P50 ≤ 800 ms, P95 ≤ 3 s, P99 ≤ 8 s (GET iccid) · P95 ≤ 5 s (provider-scoped listing / routing-map page) |
| Availability | 99.5% mensual; caída de un proveedor no genera 5xx para SIMs de otros |
| Scalability | 20 concurrentes en una instancia API; escalar horizontalmente requiere mover rate limits estrictos a Redis si el TPS de proveedores lo exige |
| Security | CORS explícito · refresh tokens hasheados · Fernet en credenciales · lifecycle writes auditados · rate limit 60 req/s por tenant pendiente |
| Observability | `request_id` middleware y `structlog` activos · métricas Prometheus y trazas OTel pendientes |
| Maintainability | Cobertura 80% global, 90% mappers · import-linter contratos · nuevo provider sin tocar dominio |
| Cost | 1 servicio API + Postgres + Redis broker + worker async; sin caché de detalles en Redis |

**Blockers ya cerrados**:
- **NFR-Sec2**: refresh tokens se guardan como sha256.
- **NFR-Sec3**: CORS usa `settings.cors_origins`, no `*` con credentials.

**Pendientes antes de endurecer producción**:
- Rate limiting por tenant.
- Métricas/alertas operativas.
- Auditoría genérica para rotación/desactivación de credenciales y denegaciones 403; los writes de SIM ya usan `lifecycle_change_audit`.

---

## Security Architecture

Detalle en [nfr-analysis.md §2](nfr-analysis.md) y [ADR-008](adrs/ADR-008-auth-rbac-audit.md).

| Capa | Mecanismo |
|---|---|
| AuthN | JWT HS256 propio (60 min) + refresh tokens rotatorios hasheados + bcrypt en passwords |
| AuthZ | RBAC por `AppRole` + tenant isolation obligatorio por `company_id` |
| Data in transit | TLS 1.2+ terminado en el orquestador; HSTS |
| Data at rest | SIM data no persiste. Credenciales de proveedor cifradas con Fernet usando `FERNET_KEY`. Refresh tokens sha256. |
| Secrets | `JWT_SECRET`, `DATABASE_URL`, `FERNET_KEY` en env vars gestionadas por orquestador |
| Log hygiene | Scrubber obligatorio sobre campos `password`, `token`, `Authorization`, `credentials`, `secret`, `key` |
| Audit | Writes de SIM → `lifecycle_change_audit`; `audit_log` existe para auditoría genérica, pero falta middleware/decorador general para todas las mutaciones y 403 |
| Abuse control | Rate limit por `company_id` (token bucket) · `Idempotency-Key` obligatorio en `POST /v1/sims/{iccid}/purge` |

**Matriz de permisos** (ADR-008):

| Operación | member | manager | admin |
|---|:-:|:-:|:-:|
| Lecturas sobre SIMs | ✓ | ✓ | ✓ |
| Ver/probar/rotar credenciales propias del tenant | ✗ | ✓ | ✓ |
| Descubrir subcompañías Moabits | ✗ | ✓ | ✓ |
| Seleccionar mapping Moabits | ✗ | ✗ | ✓ |
| Desactivar credenciales del tenant | ✗ | ✗ | ✓ |
| Control operation `purge` | ✗ | ✗ | ✓ |

---

## Data Architecture

**Principio**: **cero persistencia** del estado de SIM (ADR-002). Postgres aloja sólo:

| Tabla | Propósito | Contexto |
|---|---|---|
| `users`, `profiles`, `refresh_tokens` | Identity & Access | existente |
| `companies`, `company_settings` | Tenancy | existente |
| `company_provider_credentials` | Credenciales cifradas por (Company × Provider) | **nueva** — ADR-006 |
| `moabits_source_companies` | Cache de subcompañías Moabits descubiertas para mostrar opciones de selección | **nueva** — ADR-010 |
| `company_provider_mappings` | Mapping explícito Company local ↔ cuenta/subcompañía nativa del proveedor; en Moabits contiene el `provider_company_code` operativo | **nueva** — ADR-010 |
| `sim_routing_map` | `iccid → provider, company_id, last_seen_at` | **nueva** — ADR-002 |
| `audit_log` | Bitácora de mutaciones y denegaciones | **nueva** — ADR-008 |
| `idempotency_keys` | `(company_id, key)` → respuesta cacheada 24 h | **nueva** — ADR-007 |
| `lifecycle_change_audit` | Bitácora específica de writes de lifecycle/purge | **nueva** — implementación |

**Modelo de ruteo**: el único estado compartido sobre SIMs es el `SimRoutingMap`. No es caché, no es espejo, es **ruteo** (qué proveedor atender para este iccid). Se puebla por `POST /v1/sims/import` o por lazy upsert cuando se ejecuta un listing provider-scoped exitoso (`GET /v1/sims?provider=<name>`).

**Índices críticos**:
- `sim_routing_map(iccid PK)` — lookup O(1).
- `sim_routing_map(company_id, provider)` — listado por tenant.
- `company_provider_credentials(company_id, provider) WHERE active` — resolución de credencial.
- `audit_log(company_id, occurred_at DESC)` — consultas de auditoría por tenant.
- `audit_log(actor_id, occurred_at DESC)` — actividad por usuario.
- `idempotency_keys(company_id, key)` — replay idempotente por tenant.
- `lifecycle_change_audit(provider, requested_at)` — diagnóstico de writes por proveedor.

---

## API Design

### Endpoints (detalle en [ADR-007](adrs/ADR-007-api-versioning-and-pagination.md))

```
POST   /v1/auth/login
POST   /v1/auth/signup
POST   /v1/auth/refresh
POST   /v1/auth/logout
GET    /v1/me
GET    /v1/users
*      /v1/companies/**
GET    /v1/companies/me/credentials                     # manager/admin — metadata only
GET    /v1/companies/me/credentials/{provider}          # manager/admin — metadata only
POST   /v1/companies/me/credentials/{provider}/test     # manager/admin — no secret persistence
PATCH  /v1/companies/me/credentials/{provider}          # manager/admin — rotate/create
GET    /v1/companies/me/provider-mappings/moabits               # manager/admin — own Moabits mapping
GET    /v1/companies/provider-mappings/moabits                  # admin — all local Moabits mappings
GET    /v1/companies/provider-mappings/moabits/source-companies # admin — cached Moabits choices
GET    /v1/companies/provider-mappings/moabits/discover         # admin — live Moabits discovery, refreshes cache
PUT    /v1/companies/{company_id}/provider-mappings/moabits     # admin — configure Moabits mapping
DELETE /v1/companies/me/credentials/{provider}          # admin — deactivate
POST   /v1/admin/companies/{company_id}/credentials/{provider}/probe  # admin — stored credential smoke test
GET    /v1/providers/{provider}/capabilities             # supported / not_supported / feature-flag / confirmation

GET    /v1/sims?provider=<name>                          # provider-scoped listing required by default
GET    /v1/sims/{iccid}
GET    /v1/sims/{iccid}/usage                             # normalized usage_metrics + provider_metrics
GET    /v1/sims/{iccid}/presence
PUT    /v1/sims/{iccid}/status                            # Idempotency-Key obligatoria · admin
POST   /v1/sims/{iccid}/purge                             # Idempotency-Key obligatoria · admin
POST   /v1/sims/import                                    # bootstrap SIM Routing Map
```

### Convenciones

- Versionado en URL (`/v1/`), nunca por header.
- La implementación actual usa rutas REST explícitas sobre `/v1/sims/...`; la operación canónica sigue siendo `purge`, pero no se adoptó `:` custom verbs en este repo.
- No se exponen endpoints proveedor-específicos para `networkReset`, `Edit Device Details` o rutas de purga propietarias; los adapters traducen desde `POST /v1/sims/{iccid}/purge`.
- Las capacidades que existan sólo en un proveedor se publican como capabilities opcionales, no como lógica condicional en el frontend.
- `status_history`, `aggregated_usage`, `plan_catalog` y `quota_management` se reportan en `GET /v1/providers/{provider}/capabilities`; no tienen ruta pública en v1 hasta que se adopte un Capability Protocol y se implemente el router correspondiente.
- Paginación cursor-based (ordenamiento estable cross-proveedor).
- Errores RFC 7807 con `code` estable (`provider.unavailable`, `subscription.not_found`, …).
- Headers: `X-Request-ID` echo-or-generated, `X-API-Version: v1`.
- `Idempotency-Key` obligatoria en mutaciones; sin ella → 400.
- Respuestas parciales en búsquedas: `200` con `partial: true` y `failed_providers[]`.

### OpenAPI

Generado automáticamente por FastAPI en `/v1/openapi.json` y `/v1/docs`. Schemas nombrados para `Problem`, `Page<Subscription>`, `Subscription`, `UsageSnapshot`, etc.

---

## Testing Strategy

Detalle en [ADR-009](adrs/ADR-009-testing-strategy.md).

Pirámide:

1. **Unit sobre mappers con golden files** — el activo principal. Payload del proveedor ↔ modelo canónico esperado, comparados con `assert ==`. Cambio del proveedor = falla determinista.
2. **Contract tests sobre adapters** — cumplimiento del Protocol + comportamiento de errores (401→ProviderAuthFailed, 429→ProviderRateLimited, timeout→ProviderUnavailable, etc.).
3. **Integration tests sobre services** — con `FakeProvider` in-memory + Postgres real.
4. **Component tests sobre routers** — `TestClient` + dependency overrides.
5. **E2E opcional** — contra sandbox de proveedor, CI nightly, no bloqueante. `[REQUIRES INPUT]`
6. **Property-based** opcional con `hypothesis` en mappers críticos.

**Gates de CI**: cobertura 80% global, 90% mappers, 100% errores/auth. Suite completa (capas 1–4) < 60 s.

---

## Cost Architecture

Detalle en [patterns-decisions.md D7](patterns-decisions.md) y [ADR-005](adrs/ADR-005-resilience-and-cache.md).

**Palancas de costo**:
1. **Sin fan-out cross-provider por defecto.** Single-SIM y mutaciones llaman sólo al proveedor resuelto; listados usan listing nativo provider-scoped o una página acotada de `sim_routing_map`.
2. **Infra acotada**: 1 servicio API + Postgres + Redis (broker) + worker async para sync de inventario y exports (ver ADR-012). **No** se introduce caché de detalles en Redis: el detalle sigue siendo proxy puro en vivo (ADR-002 §preservado).
3. **Rate limit por `company_id`** protege la cuota del tenant y evita que un cliente descontrolado queme su propia cuota o la de otros.

**Drivers de costo a monitorear**:
- Cuota/costo por request de cada proveedor (depende del contrato).
- Egress de red hacia los proveedores (menor a la escala actual).
- Postgres: trivial — las tablas son chicas (134k routing rows, audit_log creciente, idempotency de 24 h).

**Tele2 / Cisco Control Center fair use aplicado**:
- Search Devices exige `modified_since` en formato `yyyy-MM-ddTHH:mm:ssZ`; si falta, se devuelve `errorCode=10000003`.
- `pageSize` default/máximo = 50, `pageNumber` default = 1, y `modifiedTill` default = `modifiedSince + 1 año`.
- Tras recibir una página de Search Devices, el adapter llama `Get Device Details` únicamente para las primeras 5 SIMs de esa página. Esas filas se responden con `detail_level=detail`; el resto conserva la información de Search Devices y se responde con `detail_level=summary`.
- La respuesta pública se normaliza antes de salir del router: `ratePlan`/`communicationPlan`, `accountId`, IPs, hardware/profile ids, fechas y `accountCustom*` se proyectan a bloques `normalized` cuando están disponibles.
- Tele2 se rate-limita en proceso por cuenta/tenant: default 1 TPS, configurable con `company_provider_credentials.account_scope.max_tps` (p.ej. `5` para Advantage).
- `40000029 Rate Limit Exceeded` y HTTP 429 se traducen a `ProviderRateLimited` y activan backoff temporal.

**Tele2 / Cisco Control Center pendiente**:
- El rate limiter no es distribuido. Usar múltiples workers/réplicas puede violar el TPS contratado; migrar a Redis antes de escalar horizontalmente.
- No hay detección automática de BCTPS/SBCTPS/ICTPS/Overage TPS; `max_tps` se configura manualmente.
- No hay caché diaria/por cadence para evitar búsquedas repetidas; el cliente debe limitar la frecuencia funcional de sincronización.

**Cuándo esto deja de ser óptimo**:
- Si se introducen reportes cross-proveedor → Postgres va a recibir carga grande de lectura → mover a modo agregador (implica invalidar ADR-002).
- Si se escala a N workers de API en paralelo, el cache per-proc deja de ser óptimo → mover el cache a Redis (que ya existe como infra desde ADR-012; sigue siendo una decisión explícita y no automática).
- Si el sync worker (ADR-012) y el tráfico real coexisten en la misma ventana → migrar el rate limiter de in-proc a Redis (ver ADR-005 §revisión 2026-05-25).

---

## Deployment Architecture

- **Unidad**: imagen Docker. Un proceso uvicorn. `docker-compose.yml` ya existe para dev.
- **Env vars mínimas**:
  - `DATABASE_URL` — Postgres con SSL.
  - `JWT_SECRET` — rotable.
  - `JWT_EXPIRE_MINUTES` — default 60.
  - `FERNET_KEY` — 32 bytes URL-safe base64, **obligatorio en prod**.
  - `CORS_ORIGINS` — lista JSON. **No `["*"]` con credentials en prod.**
  - `ENVIRONMENT` — `development`|`staging`|`production`.
  - `PROVIDER_<NAME>_TIMEOUT_READ_MS`, `_RETRY_ENABLED`, `_CB_OPEN_THRESHOLD` — overrides por adapter.
  - `RATE_LIMIT_PER_TENANT_RPS` — default 60.
- **Reverse proxy** (nginx, Caddy, managed por el PaaS) para TLS, HSTS, header stripping.
- **Health**: `/health` ya existe. Agregar `/ready` que chequee Postgres disponible.
- **Rolling deploy**: 2 réplicas mínimas en prod para no tener downtime durante release (aunque una sola atienda la carga nominal).
- **Migraciones**: SQL manual en `migrations/*.sql`. Ejecutar `migrations/000_init.sql` primero y luego los archivos numerados en orden. Cada archivo crea una responsabilidad: routing, credenciales, audit log, idempotency y lifecycle audit.

---

## Migration Strategy (brownfield)

**Plan en tres olas**, cada una independientemente desplegable y reversible.

### Ola 0 — Paydown de deuda técnica bloqueante (antes de tocar nada de subscriptions)
Blockers de seguridad (NFR-Sec2, NFR-Sec3):
- CORS explícito y refresh tokens hasheados ya están implementados.
- Prefijar routers existentes con `/v1/` ([app/main.py:44-47](../../app/main.py#L44-L47)).
- Introducir logger estructurado (`structlog`) + middleware de `request_id`.
- Introducir `DomainError` + handler global RFC 7807.
- Reorganizar carpetas a paquetes por bounded context (`identity/`, `tenancy/`, `shared/`) sin cambio funcional.

### Ola 1 — Fundación del dominio de suscripciones (primer proveedor: Kite)
- DDL: `001_sim_routing_map.sql`, `002_company_provider_credentials.sql`, `003_audit_log.sql`, `004_idempotency_keys.sql`, `005_lifecycle_change_audit.sql`, `006_moabits_source_companies.sql`, `007_company_provider_mappings.sql`, `008_sync_jobs.sql`.
- `app/subscriptions/` con aggregate canónico y use cases implementados en router/helpers.
- `app/providers/base.py` con Protocol + errores.
- `app/providers/kite/` completo (client, dto, mappers, adapter) + tests Capa 1 y 2.
- Endpoints `/v1/sims/**` funcionales sólo para SIMs de Kite.
- Rate limit, circuit breaker, cache TTL, import-linter, métricas Prometheus en `/metrics`.

### Ola 2 — Tele2 y Moabits
- `app/providers/tele2/` y `app/providers/moabits/`.
- Para cada uno: golden files, contract tests, mapeo de estados, decisión explícita sobre operaciones no soportadas.
- Herramienta CLI para bootstrap del `SIM Routing Map` desde CSV.

### Ola 3 — Endurecimiento operativo
- Alertas configuradas sobre dashboards.
- Runbook para: rotar credencial de proveedor, rotar `JWT_SECRET`, abrir/cerrar circuito manualmente, investigar fallas desde `request_id`.
- OpenTelemetry si se adopta APM.

### Rollback
- Cada ola es un set de PRs. Ola 1 y Ola 2 son aditivas (nuevas tablas, nuevos endpoints). Rollback = revert y migración inversa.
- Ola 0 toca código existente → feature flag por `settings.environment`: si `development`, mantener CORS `*` para facilitar dev; en `production` es una lista.

---

## Open Questions

Capturadas durante el proceso; ninguna bloquea el diseño, algunas bloquean implementación:

| # | Pregunta | Bloquea |
|---|---|---|
| OQ-1 | `SIM Routing Map`: ¿bootstrap por CSV de los proveedores o descubrimiento lazy? | Ola 2 |
| OQ-2 | Mapeo `Company` local ↔ `endCustomerId` (Kite) / `accountId` (Tele2) / company code Moabits. ¿Estructura exacta en `account_scope` para Kite/Tele2? | Ola 1 — *Moabits resuelto en ADR-010: el mapping operativo se persiste explícitamente en `company_provider_mappings.provider_company_code` vía `PUT /v1/companies/{company_id}/provider-mappings/moabits` admin-only; sin auto-scope por nombre.* |
| OQ-3 | ¿Alguno de los proveedores ofrece sandbox para E2E? | Capa 5 de testing (opcional) |
| OQ-4 | Stack de observabilidad: ¿Prometheus/Grafana propio, Datadog, New Relic? | Ola 3 |
| OQ-5 | Retención legal de `audit_log` | Ola 3 |
| OQ-6 | ¿Frontend ya envía `Idempotency-Key`? Si no, coordinar con frontend | Ola 1 |
| OQ-7 | SLA contractual de cada proveedor — calibra timeouts y umbrales de circuit breaker | Ola 1 |
| OQ-8 | Rotación de `FERNET_KEY`: ¿procedimiento definido? | Prod |
| OQ-9 | ¿Superadmin global cross-tenant será necesario? Hoy no contemplado | Futuro |

---

## Next Steps (priorizado, accionable)

> **Nota de estado (2026-05-25)**: esta sección conserva la secuencia original de implementación como contexto histórico. Los pendientes reales están resumidos en este documento, `_context_state.json`, `PROVIDER_SPEC_GAPS.md` y ADR-012. Los planes/reportes antiguos se archivaron en `docs/archive/2026-05-provider-remediation/`.

**Orden sugerido de ejecución**:

1. **Ola 0 — deuda bloqueante** *(días, 1 dev)*
   1. [done] CORS explícito.
   2. [done] Refresh tokens hasheados.
   3. [done] Routers existentes bajo `/v1/`.
   4. [done] `structlog` + middleware `request_id`.
   5. [done] `DomainError` + handler global RFC 7807.
   6. [done] Reorganizar a paquetes por bounded context.
   7. [pending] `import-linter` con contratos.

2. **Ola 1 — primer proveedor (Kite)** *(semanas, 1–2 devs)*
   1. [done] SQL migrations: `001` a `006`.
   2. [done] `app/providers/base.py` — Protocol + errores.
   3. [done] `app/subscriptions/` — aggregate + routers/use cases.
   4. [done] `app/providers/kite/` completo + tests.
   5. [partial] Circuit breaker + `/ready` hechos; rate limit genérico, cache genérica y métricas Prometheus pendientes.
   6. [PR] Matriz RBAC + `audit_log` middleware.
   7. [PR] Endpoints `companies/me/credentials` para alta/rotación por `manager`/`admin` y desactivación por `admin`.

3. **Ola 2 — Tele2 + Moabits** *(semanas, 1 dev en paralelo)*
   - Uno por sprint. Cada proveedor: adapter + tests + CLI bootstrap de routing map.

4. **Ola 3 — endurecimiento** *(días)*
   - Alertas, runbooks, OTel opcional.

**Quick wins que se pueden aplicar hoy sin esperar la ola**:
- Fix NFR-Sec3 (CORS) y NFR-Sec2 (refresh token hash) — **minutos de código, eliminan vulnerabilidades reales**.

---

## References

| Recurso | Link |
|---|---|
| Context state canónico | [_context_state.json](_context_state.json) |
| Phase 1 — análisis del código | [arch-analysis.md](arch-analysis.md) |
| Phase 2 — modelo de dominio | [domain-model.md](domain-model.md) |
| Phase 2 — context map | [context-map.mermaid](context-map.mermaid) |
| Phase 3 — patrones y trade-offs | [patterns-decisions.md](patterns-decisions.md) |
| Phase 4 — ADRs | [adrs/](adrs/) |
| Phase 5 — C4 diagrams | [c4-context.mermaid](c4-context.mermaid), [c4-container.mermaid](c4-container.mermaid), [c4-component.mermaid](c4-component.mermaid) |
| Phase 6 — NFR analysis | [nfr-analysis.md](nfr-analysis.md) |
| Phase 7 — consistency check report | [_phase7_consistency_check.md](_phase7_consistency_check.md) |
| **Provider Spec Gaps** | [PROVIDER_SPEC_GAPS.md](PROVIDER_SPEC_GAPS.md) — Unimplemented features, missing endpoints, and product decisions for future capability protocols |

**Externos** (para referencia, no URLs inventadas — son estándares públicos bien conocidos):
- DDD Context Mapping (Eric Evans, Vaughn Vernon) — para el patrón ACL.
- C4 Model (Simon Brown).
- RFC 7807 — Problem Details for HTTP APIs.
- Google AIP-136 — custom methods / verbs con `:`.
- OWASP API Security Top 10.
