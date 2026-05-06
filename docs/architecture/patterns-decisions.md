# Phase 3 — Selección de Patrones

> Una decisión por dimensión. Cada una con: pattern elegido, alternativas consideradas, trade-offs, condiciones de invalidación. Los items marcados con (→ ADR-NNN) generarán un ADR formal en Phase 4.

---

## D1. Topología de despliegue → **Modular Monolith** (→ ADR-001)

| | |
|---|---|
| **Elegido** | Una sola aplicación FastAPI con paquetes internos por bounded context: `app/identity/`, `app/tenancy/`, `app/subscriptions/`, `app/providers/{kite,tele2,moabits}/`. Single deployable unit. |
| **Alternativas** | (a) Microservicio por proveedor + gateway · (b) Monolito layered actual sin reorganizar · (c) Serverless (FaaS) por endpoint |
| **Trade-offs** | A favor: 15–20 usuarios concurrentes y `team_size < 5` no justifican microservicios (anti-pattern explícito en CLAUDE.md §6). Operación más simple (un servicio, un deploy, un log stream). Refactor barato hoy. En contra: un proveedor lento puede saturar el event loop si no hay límites por adapter (mitigable con `asyncio.Semaphore` por proveedor). |
| **Invalida la decisión si** | (i) crece a >50 usuarios concurrentes con perfiles de carga muy distintos por proveedor, o (ii) el equipo crece a >2 squads independientes que necesitan deploys desacoplados, o (iii) un proveedor exige aislamiento de red/IP allowlist tan distinto que pesa más separar. |

---

## D2. Estilo de comunicación con proveedores → **HTTP síncrono + concurrencia con `asyncio.gather`** (→ ADR-005)

| | |
|---|---|
| **Elegido** | `httpx.AsyncClient` por adapter y requests síncronos al proveedor. El backend evita fan-out cross-provider por defecto: usa `sim_routing_map` para single-SIM/global page y listing nativo cuando el cliente pasa `?provider`. `asyncio.gather(return_exceptions=True)` queda reservado para composición interna documentada del adapter. Timeout configurable por adapter, retry con backoff exponencial sólo en errores idempotentes (GET) y 5xx/timeouts. **Circuit breaker** por adapter para no propagar fallas. |
| **Alternativas** | (a) Cola intermedia (Celery/Arq) — overkill para proxy en tiempo real · (b) Sin retries — degrada UX por flapping del proveedor · (c) Streaming/SSE — los proveedores no lo ofrecen |
| **Trade-offs** | A favor: la API es proxy puro; cualquier asincronía agrega complejidad sin reducir latencia (el cliente sigue esperando). En contra: la latencia del peor proveedor es el techo. Mitigación: timeout duro (ver NFR §latency) + cierre de circuito que devuelve `503 ProviderUnavailable` rápido en vez de esperar el timeout completo. |
| **Invalida la decisión si** | El volumen de operaciones de control (purge) crece a punto de necesitar encolar y reintentar offline. |

---

## D3. Propiedad de los datos → **Sin propiedad** (proxy puro) + **tabla de routing** (→ ADR-002)

| | |
|---|---|
| **Elegido** | El estado del SIM vive **sólo** en el proveedor. La API no tiene tabla `subscriptions`, no tiene snapshot, no hay sync nocturno. Lo único persistido sobre SIMs es `sim_routing_map(iccid → provider, company_id, last_seen_at)` para evitar fan-out por defecto. |
| **Alternativas** | (a) Replicar todo el catálogo en Postgres y sincronizar — modo "agregador" — explícitamente descartado por el usuario · (b) Sin routing map: descubrir provider por fan-out a los 3 — duplica latencia y cuota de proveedor |
| **Trade-offs** | A favor: cero problemas de consistencia eventual; cero deuda de pipelines; mínima superficie de mantenimiento. La fuente de verdad nunca es ambigua. En contra: cada request al cliente cuesta una request al proveedor (sin caché) — mitigado con TTL ≤ 5 s sólo para de-duplicar concurrentes. No se puede hacer búsquedas globales offline ni reportería sobre el catálogo. |
| **Invalida la decisión si** | El producto necesita reportes históricos cross-proveedor, búsquedas full-text sobre 134k SIMs en <100 ms, o trabajar offline cuando el proveedor cae. Si eso pasa, hay que pasar al modo agregador y eso es una **arquitectura distinta** (event-driven sync + Postgres canónico). |

---

## D4. Estilo de API expuesta → **REST/JSON + URL versioning + cursor pagination** (→ ADR-007)

| | |
|---|---|
| **Elegido** | REST con prefijo `/v1/`. Recurso central `/v1/sims`. Paginación cursor-based (`?cursor=...&limit=50`) para mantener contrato estable entre listing provider-scoped y página global vía routing map. Errors siguen RFC 7807 (Problem Details). OpenAPI auto-generado por FastAPI. |
| **Alternativas** | (a) GraphQL — overkill, agrega tipado pero los proveedores ya devuelven JSON estructurado · (b) gRPC — el frontend probablemente sea web; agrega fricción · (c) sin versionar — bloqueado: el modelo canónico va a evolucionar |
| **Trade-offs** | A favor: ecosistema maduro, OpenAPI gratis, cliente puede ser cualquier cosa. En contra: cursor pagination tiene más complejidad de cliente que offset. |
| **Invalida la decisión si** | Aparece un caso fuerte de cliente móvil con ancho de banda crítico que justifique GraphQL/gRPC. |

---

## D5. Autenticación / Autorización → **JWT existente + RBAC + scope por Company** (→ ADR-008)

| | |
|---|---|
| **Elegido** | Reutilizar el JWT propio (PyJWT, HS256) ya implementado en `app/dependencies.py`. RBAC por `AppRole`: lectura abierta a `member` y superiores; mutaciones (`purge`) restringidas a `admin`. Toda operación implícitamente scoped a `Profile.company_id`. **Auditoría** obligatoria para mutaciones (tabla `audit_log` nueva). |
| **Alternativas** | (a) Migrar a Supabase Auth nativo o Auth0 — costo de migración alto, no hay justificación ahora · (b) API key por compañía — débil para multi-tenant con roles |
| **Trade-offs** | A favor: cero cambio infra, ya funciona. En contra: HS256 con secreto compartido — si se filtra el secreto, se puede forjar cualquier token. Mitigación: rotación documentada + paso futuro a RS256 con kid. **Refresh tokens en plano (AP-8) son un riesgo de seguridad que debe mitigarse antes de exponer a Internet.** |
| **Invalida la decisión si** | La empresa adopta SSO corporativo (SAML/OIDC) o necesita federación de identidad. |

---

## D6. Estrategia de testing → **Pirámide + contract tests por adapter + golden files** (→ ADR-009)

| | |
|---|---|
| **Elegido** | (i) **Unit** sobre mappers de cada adapter usando *golden files* — payloads JSON de respuesta del proveedor capturados como fixtures, mappeados al modelo canónico, y comparados contra una salida esperada. Esto **es la red de seguridad principal**: cualquier cambio sutil del proveedor rompe el test. (ii) **Contract tests** del Provider Adapter: tests que validan que cada adapter respeta el `Protocol SubscriptionProvider` (mismas firmas, mismas excepciones). (iii) **Integration tests** del routing y los services con un *fake provider* in-memory. (iv) **Component tests** sobre los routers con FastAPI TestClient + adapters fakes. (v) **End-to-end opcional** contra sandbox del proveedor sólo en CI nightly si los proveedores ofrecen sandbox. |
| **Alternativas** | (a) E2E-heavy contra sandbox real — flaky, costo y rate-limit del proveedor · (b) Sólo unit — no detecta drift de contrato del adapter |
| **Trade-offs** | A favor: la pirámide se mantiene rápida; los golden files son trivialmente actualizables (capturás un nuevo payload, regenerás). En contra: requiere disciplina para refrescar fixtures cuando el proveedor cambia. |
| **Invalida la decisión si** | Los proveedores ofrecen un sandbox confiable y rápido — ahí mover algunos tests a integración real. |

---

## D7. Modelo de costos / FinOps → **Tres palancas: evitar fan-out, caché in-memory, throttling por tenant**

| | |
|---|---|
| **Elegido** | (i) Cada credencial de proveedor probablemente tiene cuota o costo por request. La política es: **single-SIM y mutaciones sólo llaman al proveedor resuelto; listados usan listing nativo provider-scoped o una página acotada desde `sim_routing_map`**. (ii) Caché L1 in-memory por adapter, TTL ≤ 5 s, sólo para de-duplicar concurrentes (stampede prevention). (iii) Throttling por `company_id` (no global) usando un token bucket en memoria — protege la cuota del tenant frente a clientes mal portados sin penalizar al resto. |
| **Alternativas** | (a) Caché Redis con TTL largo — tienta a "almacenar" y rompe la promesa de proxy puro · (b) Sin caché — tormentas de stampede triviales · (c) Throttling global — penaliza injustamente a tenants pequeños |
| **Trade-offs** | A favor: cero infra adicional (Redis no requerido en MVP), comportamiento predecible. En contra: en una caída del proceso se pierde el bucket de throttling (aceptable: 15–20 concurrentes, el daño es contenido). |
| **Invalida la decisión si** | Aparecen patrones de carga ráfaga > 100 req/s sostenidos, o se escala a múltiples instancias del API que requieran throttling compartido (ahí Redis + `slowapi` o `aiocache` distribuido). |

---

## D8. Resolución de credenciales y secretos → **Tabla cifrada + KMS/pgcrypto, NO `company_settings.settings`** (→ ADR-006)

| | |
|---|---|
| **Elegido** | Tabla `company_provider_credentials` (migration `002_company_provider_credentials.sql`). Campo `credentials_enc` cifrado en aplicación con `FERNET_KEY` (`cryptography.Fernet`). Secrets de la app (JWT, Fernet key, DB password, eventuales tokens globales si los hay) en variables de entorno gestionadas por el orquestador del despliegue (Docker secrets / Vault / sistema gestionado del PaaS). |
| **Alternativas** | (a) Guardar credenciales en `company_settings.settings` JSONB — viola least-privilege (cualquier query de settings expone secretos) · (b) Vault/KMS dedicado — feature flag para el día que la empresa lo adopte; hoy es over-engineering |
| **Trade-offs** | A favor: simple, auditable (tabla dedicada con `rotated_at`), aislada del resto de settings. En contra: rotación manual hasta integrar KMS. |
| **Invalida la decisión si** | La empresa adopta Vault/AWS KMS/GCP Secret Manager — ahí migrar. |

---

## D9. Modelo de errores → **Jerarquía canónica + handler global a Problem Details (RFC 7807)** (→ ADR-004)

| | |
|---|---|
| **Elegido** | Jerarquía Python: `DomainError` → {`SubscriptionNotFound`, `UnsupportedOperation`, `ProviderUnavailable`, `ProviderRateLimited`, `ProviderAuthFailed`, `PartialResult`, `InvalidICCID`}. Cada adapter atrapa errores HTTP del proveedor y los traduce a una de estas. Un único `exception_handler` global serializa a Problem Details. Códigos HTTP fijos por tipo (404, 409, 503, 429, 502, 207-multi-status, 400). |
| **Alternativas** | (a) Propagar `HTTPException` de FastAPI con strings — opaco para el cliente · (b) Errores específicos por proveedor — filtra vocabulario externo al cliente |
| **Trade-offs** | A favor: el cliente ve siempre el mismo formato; el código del router no contiene branching por proveedor. En contra: una capa más de mapeo. |
| **Invalida la decisión si** | n/a — es un trade-off aditivo. |

---

## D10. Observabilidad → **Logs JSON estructurados con `request_id` + métricas Prometheus + tracing OpenTelemetry**

| | |
|---|---|
| **Elegido** | (i) `structlog` o `loguru` con middleware FastAPI que inyecta `request_id` y `tenant_id` en contexto. Cada llamada al adapter loguea `provider`, `operation`, `latency_ms`, `outcome`. (ii) Métricas Prometheus expuestas en `/metrics`: counters por provider × outcome, histogramas de latencia por provider × operation, gauge de circuit breaker state. (iii) OpenTelemetry **opcional** — trazas distribuidas hacia los proveedores externos si se exporta a un backend. |
| **Alternativas** | (a) Logs sólo a stdout sin estructura — irrastreable cuando un cliente reporte un caso · (b) APM cerrado (Datadog/NewRelic) — válido si la empresa ya lo paga |
| **Trade-offs** | A favor: con tres proveedores, **la correlación es la diferencia entre depurar en minutos o en horas**. Métricas separan claramente "fallé yo" de "falló Tele2". En contra: un poco más de boilerplate inicial. |
| **Invalida la decisión si** | n/a — esto es no-negociable para esta arquitectura. |

---

## Resumen — decisiones que generan ADR (Phase 4)

| # | Decisión | Origen |
|---|---|---|
| ADR-001 | Modular Monolith vs microservicios | D1 |
| ADR-002 | Real-time proxy + SIM Routing Map (sin canonical store) | D3 |
| ADR-003 | Anti-Corruption Layer + Adapter por proveedor + Provider Registry | D1 + D2 (transversal) |
| ADR-004 | Modelo de errores canónico → RFC 7807 | D9 |
| ADR-005 | Resiliencia: timeout + retry + circuit breaker + caché TTL ≤ 5 s | D2 + D7 |
| ADR-006 | Credenciales por tenant en tabla cifrada (no JSONB) | D8 |
| ADR-007 | Versionado URL `/v1/` + cursor pagination + RFC 7807 | D4 |
| ADR-008 | Reutilizar JWT existente + RBAC + scope por `Company` + auditoría de mutaciones | D5 |
| ADR-009 | Pirámide de tests + golden files de mappers + contract tests | D6 |

Update `_context_state.json.decisions_made` con estos 9 ítems al cerrar Phase 3.
