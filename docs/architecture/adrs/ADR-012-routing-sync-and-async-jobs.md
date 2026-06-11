# ADR-012 — Sync periódico del routing + cola para jobs asíncronos

- **Estado**: Accepted — partially implemented (fases A/B/C)
- **Fecha**: 2026-05-25
- **Decisores**: equipo backend
- **Relacionado**: ADR-001 (modular monolith), ADR-002 (proxy puro — **principio preservado**, solo cambia el mecanismo de carga del routing), ADR-005 (resiliencia / rate limiter — coordinación), ADR-007 (paginación), ADR-011 (Moabits v2 enrichment — el worker reutiliza el mismo path)

## Contexto

ADR-002 estableció el sistema como **proxy puro**: cero persistencia del estado de SIM, sólo `sim_routing_map(iccid → provider, company_id, last_seen_at)` para evitar fan-out en single-SIM. La carga del routing se hacía por (i) CSV bootstrap, (ii) provider-scoped discovery on-demand vía `GET /v1/sims?provider=<name>`, (iii) lazy cross-provider fan-out al primer `GET /v1/sims/{iccid}`.

Tres nuevos casos de uso han aparecido que el modelo actual cubre mal:

1. **Listado paginado global de SIMs** (tamaños 10/25/50/100) sin esperar a los providers en cada request. Sin un routing pre-poblado, "página global" obliga a fan-out o a lazy discovery por cada ICCID nuevo.
2. **Filtros por lista de ICCIDs de múltiples proveedores**: el usuario envía 50–200 ICCIDs mezclados; el sistema debe devolver los detalles correctos sabiendo a qué provider pedirle cada uno. Sin routing previo, esto exige fan-out exploratorio que amplifica requests y rompe TPS de Tele2.
3. **Export masivo de detalle** (miles de SIMs a CSV/JSON): tarda minutos a horas a 60 TPS de Tele2; no se puede esperar síncronamente sobre HTTP.

Estimación de volumen (ver `_context_state.json`): ~134k SIMs totales, ~134k de ellas en Moabits, distribución desconocida pero relevante en Tele2. Un sync completo de Tele2 a 60 TPS = **~37 min** si requiere detail por SIM. Es un workload que justifica trabajo asíncrono — no para el path de detalle, sí para inventario.

El principio de ADR-002 ("la verdad del estado de SIM vive sólo en el proveedor") se **preserva**. Lo que cambia es:
- el routing map ahora se pobla activamente, no sólo bajo demanda;
- aparecen endpoints adicionales que orquestan fan-out controlado para batch y export.

## Decisión

### 1. `sim_routing_map` permanece minimal

Sin cambios de schema respecto al estado actual:

```
sim_routing_map
  ├── iccid        TEXT      (PK)
  ├── provider     TEXT      (NOT NULL)
  ├── company_id   UUID      (NOT NULL, FK companies)
  └── last_seen_at TIMESTAMPTZ
```

**No** se agregan `status`, `msisdn`, `imsi`, `plan`, `usage`, `presence`, ni ningún otro estado. El worker descarta cualquier dato que el `listSIM` del proveedor devuelva por encima de la identidad.

**Razón**: cachear estado rompería ADR-002 y abriría stale-data bugs cada vez que la UI muestre un valor que el provider ya cambió. El detalle sigue siendo siempre live.

### 2. Sync worker periódico para poblar el routing

| Aspecto | Decisión |
|---|---|
| **Stack** | Arq (queue + scheduler async-native) sobre Redis. Sin Celery — overkill para stack 100% asyncio. |
| **Schedule** | Cron diario `02:00 UTC` por defecto, configurable vía `SYNC_CRON_EXPR`. |
| **Trigger manual** | `POST /v1/sync/trigger?provider={kite\|tele2\|moabits}` (admin role). |
| **Estado de jobs** | Tabla `sync_jobs(id, kind, provider, company_id, triggered_by, status, progress_done, progress_total, cursor, result_url, result_expires_at, errors_json, params_json, timestamps)` para resumibilidad, polling y observabilidad. |
| **Credenciales** | El worker carga credenciales con `_load_credentials` (mismo path que la API), que inyecta el scope per-provider — incluido el `company_code` de Moabits desde el mapping activo. Sin él, `MoabitsAdapter.list_subscriptions` devuelve 0 SIMs y el job terminaría "done" sin sincronizar nada. |
| **Resumibilidad** | Si un sync se interrumpe, el siguiente arranca desde `cursor` (modified_since para Tele2, offset/pageToken para Kite/Moabits). |
| **Concurrencia** | 1 worker por provider en momentos de sync (semáforo en Arq). Evita multiplicación accidental del budget TPS. |
| **Dedup / jobs abandonados** | El trigger manual y el cron saltan/rechazan (`409 SyncAlreadyRunning`) si ya hay un job `pending\|running` para ese `(company, provider)`. Los jobs `pending\|running` con antigüedad > 2 h (el techo `job_timeout` es 1 h) se consideran abandonados y se marcan `failed` automáticamente, para que no bloqueen futuros syncs. Si el `enqueue` a Redis falla tras crear la fila, el job se marca `failed` (la API responde `503`) en lugar de quedar `pending` indefinidamente. |
| **Rate limit** | El worker reutiliza el `RateLimiter` per-proc del adapter (mismo de ADR-005). Si en algún momento corren API + worker contra el mismo provider, comparten el budget — aceptable mientras haya 1 réplica del worker. |
| **Observabilidad** | Logs estructurados + métricas: `sync_duration_seconds{provider}`, `sync_iccids_seen{provider}`, `sync_errors_total{provider,kind}`. |

### 3. Endpoint batch para detalles

```
POST /v1/sims/details
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "iccids":    ["8946...01", "8946...02", "8934...03", ...],
  "providers": ["kite", "tele2"]                                // opcional
}
```

**Reglas de validación**:
- `len(iccids)` entre 1 y `MAX_BATCH_SIZE = 200`. Más que eso → `413 Payload Too Large` con mensaje "use POST /v1/sims/export".
- `providers`: si se omite, no filtra. Si se incluye, ICCIDs cuyo provider (según routing) no esté en la lista van a `filtered_out`.
- Auth: scope automático por `Profile.company_id` (igual que el resto de `/v1/sims/**`).

**Respuesta** (`200 OK`, siempre — los errores son per-ICCID, no globales):

```json
{
  "results": {
    "8946...01": { "provider": "kite",    "status": "ok",        "data": { /* SIM canónica */ } },
    "8946...02": { "provider": "tele2",   "status": "not_found", "error": { "code": "provider.resource_not_found", "detail": "provider returned 404" } },
    "8946...03": { "provider": "moabits", "status": "timeout",   "error": { "code": "provider.unavailable", "detail": "provider timeout after 8s" } },
    "8946...04": { "provider": "tele2",   "status": "ok",        "data": { /* ... */ } }
  },
  "summary":      { "ok": 2, "not_found": 1, "timeout": 1, "error": 0, "total": 4 },
  "unresolved":   ["8946...05"],
  "filtered_out": ["8946...06"]
}
```

**Status posibles por ICCID**: `ok | not_found | timeout | error | rate_limited`.

**Algoritmo**:

1. **Resolver cada ICCID** con el mismo mecanismo del single-SIM endpoint:
   routing exacto → `sim_routing_prefix_map` → lazy discovery cross-provider con
   adapters que soportan filtro `iccid`.
2. **Aplicar filtro `providers`** si vino: las que no matchean → `filtered_out`.
3. **Marcar `unresolved`** si routing/prefix/lazy discovery no encuentra owner.
4. **Agrupar por provider** los ICCIDs ya enrutados.
5. **Fan-out paralelo por ICCID/provider**: el backend llama `get_subscription`
   del adapter dueño para cada ICCID resuelto. Cada provider call pasa por los
   controles propios del adapter (`RateLimiter`, timeout, circuit breaker).
6. **Agregar y responder** con error por ICCID, nunca all-or-nothing.

**Por qué la fan-out paralela no rompe TPS**: cada adapter tiene su limiter independiente. Tele2 con 100 ICCIDs serializa internamente respetando 60 TPS; Kite y Moabits con sus listas corren en paralelo a Tele2 sin competir por budget.

### 4. Endpoint de export masivo asíncrono

```
POST /v1/sims/export
{
  "providers": ["tele2"],            // opcional, filtra
  "format":    "csv|json",
  "fields":    ["status", "msisdn", "data_usage", ...]   // detalle pedido por SIM
}

→ 202 Accepted
{
  "job_id":     "exp_01HXY...",
  "status_url": "/v1/jobs/exp_01HXY..."
}
```

```
GET /v1/jobs/{job_id}
→ 200 OK
{
  "job_id":      "exp_01HXY...",
  "status":      "pending|running|done|failed",
  "progress":    { "done": 4521, "total": 12340 },
  "created_at":  "2026-05-25T14:00:00Z",
  "finished_at": null,
  "result_url":  null,
  "error":       null
}
```

**Reglas**:
- El job corre en el worker, enumera todas las SIMs del routing (filtradas por `providers` si vino), y por cada una hace un detail-call respetando rate limiter.
- Resultado: archivo en blob storage (S3 / volumen local en MVP), URL firmada con TTL (default 24 h).
- Retención de `sync_jobs` y de archivos: 7 días por defecto, configurable.
- Auth: admin role only.

### 5. Endpoints de observabilidad del sync

```
GET  /v1/sync/status                  // último sync por provider, freshness del routing
GET  /v1/jobs/{job_id}                // detalle de un job async
POST /v1/sync/trigger?provider=tele2  // dispara manual (admin only)
```

### 6. Infraestructura nueva

`docker-compose.yml` agrega:

- `redis`: imagen oficial, persistencia `appendonly` para sobrevivir restarts; healthcheck via `redis-cli ping`.
- `worker`: misma imagen que `api`, distinta entrypoint (`arq app.sync.worker.WorkerSettings`); depends_on redis + db.

`pyproject.toml` agrega: `arq`, `redis`.

Variables de entorno nuevas:
- `REDIS_URL` (ej. `redis://redis:6379/0`)
- `SYNC_CRON_EXPR` (default `0 2 * * *`)
- `EXPORT_RESULT_TTL_HOURS` (default `24`)
- `MAX_BATCH_DETAILS` (default `200`)

## Consecuencias

**Positivas**

- Listado paginado global y filtros por lista de ICCIDs resuelven con 1 query SQL + 1 fan-out controlado, sin amplificar requests al proveedor.
- TPS de Tele2 deja de ser bloqueante en el caso "el frontend abrió la app y pidió la primera página".
- Export masivo es viable sin bloquear la API.
- El sync corre en horas valle (02:00), no compite con tráfico real.
- Se preservan los principios de ADR-002 (proxy puro para detalles) y ADR-001 (modular monolith — el worker es un proceso adicional dentro del mismo módulo).

**Negativas / mitigaciones**

- **+2 servicios en `docker-compose.yml`** (`redis`, `worker`) → más superficie ops. *Mitigación*: ambos son maduros y ligeros; healthchecks + métricas básicas; runbook breve incluido en docs.
- **Sync worker y API comparten el rate limiter per-proc** → si por error se ejecutan a la vez contra el mismo provider, compiten por budget. *Mitigación*: cron en hora valle + flag `SYNC_PAUSE_ON_PEAK` opcional + alarma si overlap supera N segundos.
- **Routing puede estar atrasado entre syncs** (hasta 24 h por default). *Mitigación*: el lazy fan-out exploratorio de ADR-002 sigue activo para ICCIDs nuevos; trigger manual disponible para ops.
- **Si Redis cae** los jobs encolados se pierden. *Mitigación*: aceptable — los syncs se retoman desde el último `cursor`; los exports son re-disparables. Persistencia AOF en redis reduce el riesgo de pérdida ante restart simple.
- **Costo Redis adicional**: trivial (instancia chica, sin réplicas en MVP).

## Alternativas consideradas

1. **Mantener sólo lazy fan-out (statu quo)** — requiere lazy discovery por cada ICCID nuevo; no escala para filtros masivos por lista de ICCIDs. **Rechazada** por costo en TPS de Tele2 y por UX de páginas lentas.

2. **Cachear detalles (status, msisdn, …) en DB con TTL** — rompe ADR-002, abre stale-data bugs, requiere invalidación al mutar. **Rechazada** explícitamente: el principio de proxy puro es no-negociable para el detalle.

3. **Solo CSV bootstrap, sin sync automático** — pone toda la carga operativa en el equipo; cada SIM nueva aprovisionada requiere import manual. **Rechazada**.

4. **Celery en vez de Arq** — stack sync-first, requiere wrappers `asgiref.sync_to_async`, agrega broker + result backend + beat + flower. **Rechazada** — Arq cubre todo (queue + cron) en stack async nativo, mucho menor footprint.

5. **APScheduler in-proc en vez de Arq + Redis** — no requiere infra extra; corre dentro del proceso uvicorn. **Rechazada** porque (i) bloquea workers de uvicorn si la tarea es larga, (ii) no escala a >1 réplica de API sin coordinación externa, (iii) no resuelve la cola para exports.

6. **Webhooks de los proveedores como trigger de sync incremental** — ideal pero (i) no todos los proveedores los soportan, (ii) introduce dependencia operativa adicional, (iii) sigue siendo necesario un sync full periódico para reconciliar. **Diferida**: si Tele2/Moabits exponen webhooks confiables, agregarlos como complemento al cron, no como reemplazo.

## Trade-offs explícitos

| Eje | Sync periódico + cola (elegido) | Statu quo (sólo lazy) |
|---|---|---|
| Listado paginado global | instantáneo (DB) | requiere fan-out o lazy fill por página |
| Filtro por 100 ICCIDs mixtos | 1 query + 1 fan-out por provider | hasta 300 calls exploratorios |
| Stale en routing | hasta 24 h | imposible (siempre live) |
| Stale en detalle | **nunca** (sigue live) | nunca |
| Costo proveedor | 1 sync nocturno + tráfico real | proporcional a tráfico real |
| Complejidad ops | +Redis +worker | mínima |
| Export masivo | viable (job + URL) | inviable sobre HTTP síncrono |
| TPS Tele2 bajo presión de UX | desacoplado del usuario | el usuario espera el limiter |

## Cuándo revisar

- Si volumen de SIMs por provider supera **1M**, el sync nocturno no termina en su ventana → particionar por sub-cuenta, o por `modified_since` con ventanas más cortas, o paralelizar con varios workers (e introducir limiter compartido — ver ADR-005).
- Si el negocio pide **frescura de routing < 1 h**, evaluar webhooks del provider antes de aumentar la frecuencia del cron.
- Si se escala a **>1 réplica del worker** o se necesita rate limit compartido API+worker → migrar `RateLimiter` a Redis (revisión explícita de ADR-005).
- Si el caso de uso de export se vuelve crítico (decenas por día) → blob storage dedicado, retención > 7 días, dashboard de jobs.
- Si aparece un caso de uso de **frescura inmediata del detalle masivo** (ej. "monitor en vivo de todas las SIMs") — ese es un workload distinto (streaming/push), no resuelve un cron.

## Plan de implementación

Cinco fases secuenciales; cada una entregable y testeable de forma aislada.

| Fase | Alcance | Estimación |
|---|---|---|
| **A. Infra** | Redis + worker en docker-compose; pin Arq/redis-py; `REDIS_URL` en settings; smoke test enqueue→consume noop job. | 1–2 días |
| **B. Routing sync** | Migration `sync_jobs` table; `app/sync/worker.py` con tasks por provider; reutiliza adapters existentes; endpoints `/v1/sync/trigger`, `/v1/sync/status`, `/v1/jobs/{job_id}`. | 3–5 días |
| **C. Batch detail** | `POST /v1/sims/details` con routing lookup, fan-out paralelo, fallback cascada, response per-ICCID. Tests con fake adapters. | 1–2 días |
| **D. Export** | `POST /v1/sims/export` + `GET /v1/jobs/{id}`; resultado en volumen local (MVP) o S3 (prod); URL firmada con TTL. | 3–4 días |
| **E. Docs back** | Esta ADR + revisiones a ADR-002/005 + patterns-decisions D2/D7 + ARCHITECTURE §Cost + `_context_state.json`. | 1 día |
| **F. Contract frontend** | Extender `../frontend/contract.md` con los 5 endpoints nuevos (`POST /v1/sims/details`, `POST /v1/sims/export`, `GET /v1/jobs/{id}`, `GET /v1/sync/status`, `POST /v1/sync/trigger`), schemas request/response, status codes, ejemplos. Coordinar con el equipo de front. | 1 día |

**Total estimado**: 10–15 días de un ingeniero.

**Estado 2026-05-25**: fases A, B y C implementadas. Quedan D (exports) y el consumo frontend.

**Dependencias**: las fases B/C/D dependen de A. B y C son independientes entre sí (pueden ir en paralelo si hay dos ingenieros). D depende de A pero no de B/C; puede diferirse al sprint siguiente si presiona. F debe completarse antes de que el frontend pueda consumir los nuevos endpoints (puede arrancar en paralelo a C/D una vez que los contratos estén estables).

## Impacto en el frontend

El frontend ya existe (Next.js 16 + React 19 + TanStack Query, ubicado en `../frontend/`). Es un repo independiente y codea contra `frontend/contract.md` como fuente de verdad. Implicaciones:

1. **`contract.md` debe extenderse** con los 5 endpoints nuevos antes de que el frontend pueda integrarlos. Esto es parte del entregable del back (Fase F).
2. **Patrones de UI ya soportados por el stack del front**:
   - `useQueries` de TanStack Query para fan-out paralelo cuando el frontend prefiera N calls a `GET /v1/sims/{iccid}` en vez del batch.
   - `useMutation` + `useQuery` con `refetchInterval` para polling de `GET /v1/jobs/{id}` durante exports.
   - Suspense + skeletons para el patrón "lista instantánea desde routing → fill por fila desde provider".
3. **Coordinación de deploy**: el endpoint batch reduce N HTTP requests del front a 1, pero requiere que ambos lados estén deployados. Estrategia recomendada: desplegar el back primero (endpoints aditivos, no breaking), luego el front consume cuando esté listo. No requiere `/v2/`.
