# Phase 6 — Análisis de Requisitos No Funcionales (NFR)

> **Regla**: cada requisito tiene un número medible. "Alta disponibilidad" no es un NFR; "99.5% uptime mensual" sí.
> **Contexto de escala**: 134 612 SIMs, 15–20 usuarios concurrentes, sin batch.

---

## 1. Tabla resumen de NFRs

| # | Categoría | Requisito (medible) | Estrategia | Riesgo | Estado |
|---|---|---|---|---|---|
| NFR-P1 | Performance | **P50 ≤ 800 ms, P95 ≤ 3 s, P99 ≤ 8 s** para `GET /v1/sims/{iccid}` | Timeout httpx 8 s + caché TTL≤5s single-flight + circuit breaker. Techo = proveedor. | Latencia de proveedor fuera de nuestro control | plan |
| NFR-P2 | Performance | **P95 ≤ 5 s** para `GET /v1/sims` (provider-scoped listing o página global vía routing map) | Listing nativo por provider cuando se usa `?provider`; sin `provider`, página acotada sobre `sim_routing_map` y metadata `partial` si una llamada falla. | Un proveedor lento degrada sólo las SIMs de esa página/proveedor | plan |
| NFR-P3 | Performance | **Caché reduce requests concurrentes al mismo `iccid` en ≥ 90 %** | `cachetools.TTLCache` TTL=5s con single-flight. | Caché in-proc no comparte entre workers | plan |
| NFR-A1 | Availability | **Uptime mensual ≥ 99.5 %** del componente API (excluye caídas de proveedor) | Modular monolith con `uvicorn --workers N`, health checks, restart policy, rolling deploys. | SPOF si deploy single-AZ | plan |
| NFR-A2 | Availability | **Caída de 1 proveedor no genera 5xx en requests a otros proveedores** | Circuit breaker por adapter + semáforo por adapter + errores canónicos aislados. | Config de breaker mal calibrada puede cerrar/abrir en flaps | plan |
| NFR-A3 | Availability | **Tiempo de falla rápida ≤ 200 ms cuando el breaker está abierto** | Breaker in-memory, chequeo antes de la llamada. | — | plan |
| NFR-S1 | Scalability | Soportar **20 usuarios concurrentes con P95 ≤ 3 s en singleton (1 worker)** | Stack async + connection pooling httpx + pool Postgres. | Un cambio a sync dentro del event loop degrada todo | plan |
| NFR-S2 | Scalability | Cuando se supere NFR-S1, escalar horizontalmente a **N workers** sin cambios de código | Estado en Postgres (routing, audit, idempotency); caché y breaker per-proc aceptable. | — | plan |
| NFR-Sec1 | Security | **Cero credenciales de proveedor en logs, responses, ni `company_settings`** | Tabla `company_provider_credentials` cifrada con Fernet (ADR-006) + scrubber en logger. | Dev olvida scrubber al agregar provider | plan |
| NFR-Sec2 | Security | **JWT expiración ≤ 60 min; refresh tokens hasheados en DB** | JWT existente + fix AP-8 (sha256 antes de insertar). | Filtración de DB expone refresh tokens si no se paga AP-8 | **done** |
| NFR-Sec3 | Security | **CORS explícito por origen; no `*` con `credentials=true`** | Reemplazar `allow_origins=["*"]` por lista desde `settings.cors_origins`. | Frontend mal configurado = bloqueo de navegador | **done** |
| NFR-Sec4 | Security | **Mutaciones auditadas al 100 %** con actor, request_id, outcome | Tabla `audit_log` (ADR-008) + middleware de audit en routers mutantes. | Dev olvida decorador de audit en endpoint nuevo | plan |
| NFR-Sec5 | Security | **Tenant isolation 100 %**: ningún iccid de otra Company se devuelve | Filtro por `company_id` en `SimRoutingMap`. Tests de isolation obligatorios. | Bug de query = brecha multi-tenant | plan |
| NFR-Sec6 | Security | **TLS 1.2+ en tránsito; HSTS; no HTTP plano** | Terminación TLS en el orquestador (reverse proxy / PaaS). | Config deploy fuera del scope del código | plan |
| NFR-Sec7 | Security | **Rate limiting por `company_id`**: ≤ 60 req/s sostenidos por tenant | Token bucket in-memory (`slowapi` o implementación propia). | Un tenant pequeño no debe impactar otro | plan |
| NFR-O1 | Observability | **100 % de requests con `request_id` propagado end-to-end** (header, logs, audit) | Middleware FastAPI que inyecta/echo `X-Request-ID` en `contextvars`. | — | plan |
| NFR-O2 | Observability | **Logs JSON estructurados** con: `ts`, `level`, `request_id`, `tenant_id`, `actor_id`, `path`, `provider?`, `operation?`, `latency_ms`, `outcome` | `structlog` o `loguru` + formatter JSON. | Logs viejos no estructurados serán ruido | plan |
| NFR-O3 | Observability | **Métricas Prometheus** expuestas en `/metrics`: `http_requests_total{route,status}`, `provider_request_duration_seconds{provider,operation,outcome}`, `circuit_breaker_state{provider}`, `cache_hit_ratio{provider,operation}` | `prometheus-fastapi-instrumentator` + métricas custom en el adapter base. | — | plan |
| NFR-O4 | Observability | **Alertas**: breaker abierto > 5 min; error_ratio(provider) > 10 % sobre 5 min; tenant con 429 > 1 % de sus requests; P95 total > 5 s | Config en el sistema de alertas (Grafana/Alertmanager). | Depende del stack de observabilidad del equipo | plan |
| NFR-O5 | Observability | **Trazas OpenTelemetry** con spans: http request → service → adapter → provider call | `opentelemetry-instrumentation-fastapi` + `opentelemetry-instrumentation-httpx`. Opcional pero recomendado. | — | plan |
| NFR-M1 | Maintainability | **Cobertura**: 80 % global, 90 % mappers, 100 % errores y auth | pytest + coverage.py como gate en PR. | Pérdida gradual si no se enforce | plan |
| NFR-M2 | Maintainability | **Contratos de import**: `subscriptions/` no importa adapter concreto; adapters no se importan entre sí | `import-linter` en CI. | Sin enforcement, el contrato se viola en el primer hotfix | plan |
| NFR-M3 | Maintainability | **Agregar Provider #4 no modifica código de `subscriptions/`** | Patrón Adapter + Registry (ADR-003). | Objetivo declarativo; verificable con code review | plan |
| NFR-D1 | Data integrity | **SIM Routing Map consistente** con credenciales activas del tenant (si una credencial se desactiva, sus iccids dejan de responder) | Join en SELECT: `routing_map JOIN credentials WHERE active`. | Join performance con 134k rows — trivial | plan |
| NFR-D2 | Data integrity | **Idempotency-Key evita doble ejecución** en `POST /v1/sims/{iccid}/purge` con TTL 24 h | Tabla `idempotency_keys`. | Cliente viejo no envía key → forzar 400 | plan |
| NFR-C1 | Cost | **No hacer fan-out cross-provider por defecto**; single-SIM y mutaciones llaman sólo al provider resuelto | `sim_routing_map` + provider-scoped search + caché single-flight para lecturas por ICCID | Un bug en routing dispara discovery/fan-out = cuota × 3 | plan |
| NFR-C2 | Cost | **Infra API**: 1 servicio, 1 DB, cero colas/brokers | Modular monolith (ADR-001) | — | plan |

Leyenda: `done` = implementado. `plan` = a implementar en la primera fase. `blocker` = debe pagarse antes de exponer la API a producción.

---

## 2. Arquitectura de seguridad

### 2.1 AuthN

- **JWT propio** (ADR-008), HS256, expiración 60 min, refresh rotatorio.
- **Bcrypt** para passwords (ya presente).
- **Mitigaciones implementadas** (AP-1, AP-8):
  - ✅ AP-1 CORS restrictivo — `CORS_ORIGINS` explícito en `.env` + `config.py`.
  - ✅ AP-8 Refresh tokens hasheados con sha256 en DB antes de persistir (`app/identity/auth_utils.py`).
  - Plan documentado de rotación del `JWT_SECRET` (futuro RS256 con kid).

### 2.2 AuthZ

- **RBAC por `AppRole`** (`public` / `member` / `manager` / `admin`).
- **Tenant isolation** implícito por `Profile.company_id`.
- **Matriz de permisos** definida en ADR-008 §3 — `member` sólo lectura; `manager` puede probar/crear/rotar credenciales de su propia Company; `admin` añade `purge`, cambios de estado y desactivación de credenciales.
- **Fail-closed**: `require_roles(*roles)` levanta 403 si el rol no está en la lista explícita. Nunca permitir por default.

### 2.3 Data protection

- **En tránsito**: TLS 1.2+ obligatorio (terminación fuera del proceso).
- **En reposo**:
  - Passwords: bcrypt cost factor ≥ 12.
  - Refresh tokens: sha256 hash.
  - Credenciales de proveedor: Fernet con `FERNET_KEY` en env (ADR-006).
  - Datos de SIM: **no se persisten** (modo proxy puro — ADR-002).
- **En logs**: scrubber activo sobre campos `password`, `token`, `Authorization`, `credentials`, `secret`, `key`, cualquier campo que matchee `re.IGNORECASE`. Default allowlist — si un campo nuevo contiene material sensible y no está contemplado, el dev que lo agregue tiene que extender el scrubber.

### 2.4 Secrets management

| Secreto | Dónde vive | Rotación |
|---|---|---|
| `JWT_SECRET` | env var, gestionada por orquestador | manual; procedimiento documentado (forzar logout tras rotar) |
| `DATABASE_URL` | env var | manual |
| `FERNET_KEY` | env var | manual + re-encrypt en bloque documentado |
| Credenciales de proveedor por tenant | tabla cifrada (ADR-006) | manual por endpoint, audit-logged |

Cuando la empresa adopte Vault/KMS gestionado, **sólo** se cambian las implementaciones de loading; el modelo no cambia.

### 2.5 Defensa en profundidad

- **Input validation** vía Pydantic en todos los DTOs entrantes.
- **Output validation** vía Pydantic response models — previene leak accidental de campos internos.
- **Rate limiting** por `company_id` (NFR-Sec7).
- **CORS** restrictivo por lista explícita.
- **HSTS** en reverse proxy.

---

## 3. Observabilidad

### 3.1 Logs

- Formato JSON, un evento por línea.
- Campos obligatorios: `ts`, `level`, `request_id`, `tenant_id`, `path`, `status`, `latency_ms`.
- Eventos adicionales por llamada a proveedor: `provider`, `operation`, `upstream_latency_ms`, `outcome` (`ok` | `timeout` | `rate_limited` | `auth_failed` | `protocol_error` | `unavailable` | `unsupported`).
- Middleware inyecta `request_id` en `contextvars`; logger lo incluye automáticamente sin que cada línea tenga que pasarlo.

### 3.2 Métricas

Counters:
- `http_requests_total{route, method, status}`
- `provider_requests_total{provider, operation, outcome}`

Histogramas:
- `http_request_duration_seconds{route, method}`
- `provider_request_duration_seconds{provider, operation}`

Gauges:
- `circuit_breaker_state{provider}` (0=closed, 1=half_open, 2=open)
- `cache_entries{provider}`
- `cache_hit_ratio_5m{provider, operation}`

### 3.3 Trazas (OpenTelemetry, opcional)

- Span padre: HTTP request del cliente.
- Spans hijos: service → adapter → provider HTTP call.
- Propagación de `traceparent` al proveedor si el proveedor lo acepta.

### 3.4 Alertas

| Regla | Umbral | Severidad |
|---|---|---|
| Circuit breaker abierto | > 5 min continuos | warning |
| Error ratio por proveedor | > 10 % sobre 5 min | warning |
| Error ratio por proveedor | > 30 % sobre 5 min | page |
| P95 total API | > 5 s sobre 10 min | warning |
| Tenant con 429 > 1 % de sus requests | sobre 10 min | info |
| DB pool saturado | > 80 % utilización sobre 5 min | warning |
| API 5xx ratio | > 1 % sobre 5 min | page |

`[REQUIRES INPUT: ¿el equipo tiene Prometheus/Grafana, Datadog, New Relic, o es decisión abierta?]`

---

## 4. Modelo de escalabilidad

### 4.1 Vertical

- Primera fase: 1 container, 1 worker uvicorn, async a pleno.
- Dimensionamiento inicial: 1 vCPU, 512 MB RAM. Headroom suficiente para 20 concurrentes dada la naturaleza I/O-bound.

### 4.2 Horizontal (trigger)

- Si sostenidamente se cumple **alguno** de estos, pasar a N workers o N containers:
  - CPU > 70 % sobre 15 min.
  - Event loop lag > 100 ms (via `asyncio.get_event_loop_policy` o `aiomonitor`).
  - P95 > 5 s con proveedores respondiendo OK.

### 4.3 Consideraciones al escalar a N

- **Cache per-proc**: aceptable (degradación marginal de hit ratio).
- **Circuit breaker per-proc**: aceptable (degradación gradual — mejor que ruido de sincronización).
- **Rate limiting per-proc**: aceptable si el tráfico se balancea; si es crítico, mover a Redis.
- **Connection pool DB**: ajustar `pool_size` en el engine proporcional a N workers.
- **Idempotency keys**: ya en DB, cross-proc-safe.

### 4.4 Pool de conexiones

- httpx: `httpx.Limits(max_keepalive_connections=20, max_connections=50)` por adapter.
- asyncpg (SQLAlchemy): `pool_size=10, max_overflow=10, pool_timeout=5s`.

---

## 5. Patrones de resiliencia

| Patrón | Dónde | Config |
|---|---|---|
| **Timeout** | httpx por adapter | connect=2s, read=8s, write=2s, pool=2s |
| **Retry con backoff exponencial + jitter** | httpx, GETs sólo | 3 intentos, 0.2/0.6/1.8s, jitter ±25%, errores: timeout, connect, 502, 503, 504 |
| **Circuit breaker** | por adapter | abrir: 5 fallos/30s o 50 % sobre 20; recovery: 30 s; probe en half-open |
| **Bulkhead** (semáforo) | por adapter | `asyncio.Semaphore(20)` default |
| **Cache single-flight** | por adapter | TTL ≤ 5 s en `cachetools.TTLCache` con deduplicación de futures |
| **Fallback** | listado global vía routing map | `partial=true` + `failed_providers[]` en vez de ocultar fallos de proveedor |
| **Idempotencia** | mutaciones | header `Idempotency-Key` obligatorio, TTL 24 h en `idempotency_keys` |
| **Rate limit** | por `company_id` | token bucket, 60 req/s sostenido, burst 100 |

---

## 6. Cambios que NFRs dispararon sobre decisiones previas

**Ninguno** crítico. Las NFRs reforzaron ADR-005 (resiliencia) y dieron forma concreta a los umbrales; no invalidaron ninguna decisión de Phase 2 o 3.

Un ajuste menor a ADR-005: inicialmente el documento decía "cache TTL ≤ 5 s en `get_usage`"; NFR-C1 y la semántica de consumo (no debe verse rancio) obligan a **deshabilitar** caché en `get_usage`. Actualizar ADR-005 en su revisión de Phase 7.

---

## 7. Cosas que **no** son NFR (pero aparecen frecuentemente en docs ajenas)

Lo que **no** está en la tabla porque no aplica o no tiene número medible:

- "La API debe ser fácil de usar" → deriva a documentación OpenAPI + ejemplos en README del frontend.
- "La API debe ser segura" → deriva a NFR-Sec1..7.
- "Alta disponibilidad" → NFR-A1..3 con números.

Si aparece un requisito ambiguo en conversación futura, convertirlo en NFR medible o archivarlo como `[REQUIRES INPUT]`.
