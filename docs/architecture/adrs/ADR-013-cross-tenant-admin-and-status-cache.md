# ADR-013 — Vista admin cross-tenant y cache de estado para KPIs/filtros agregados

- **Estado**: Proposed — not implemented
- **Fecha**: 2026-05-26
- **Decisores**: equipo backend + producto
- **Relacionado**: ADR-002 (proxy puro — **se revisita parcialmente**), ADR-008 (RBAC), ADR-012 (sync periódico del routing — el worker es el lugar natural para alimentar la cache propuesta), ADR-011 (Moabits v2)

## Contexto

Tres necesidades convergentes que el modelo actual no cubre:

1. **Vista admin cross-tenant**. Hoy `get_current_company_id` ([app/identity/dependencies.py:80](../../app/identity/dependencies.py#L80)) bloquea cualquier endpoint `/sims/*` a la `company_id` del caller. Un admin de plataforma (Bismark) ve únicamente las SIMs de su propia company. Para auditar/operar transversalmente debe iterar credenciales y orquestar N llamadas a providers.

2. **KPIs agregados sin caché**. `GET /v1/sims/stats` ([app/subscriptions/routers/sims.py:2031](../../app/subscriptions/routers/sims.py#L2031)) recorre páginas live contra el provider. Para Moabits con parent code, una sola llamada cubre todo. Para Tele2 (`max_tps=1`, 50k SIMs) y Kite (`maxBatchSize` ≤ 1000), recorrer el listado para contar status equivale a **decenas de minutos por refresh** — el endpoint emite `partial=true` rutinariamente.

3. **Filtros sobre toda la base**. El frontend hoy pasa filtros server-side (`operator`, `data_service`, `sms_service`, `last_lu_*`, `imei`, `imsi_list`) que el adapter aplica con `_apply_post_filters` después de paginar. La paginación del provider limita la utilidad: filtrar "operador = Claro" devuelve solo los Claro de la página actual, no los Claro de toda la base.

ADR-002 prohíbe expresamente cachear estado de SIM (msisdn, status, plan, etc.) porque "la verdad vive en el provider". ADR-012 ratificó esa restricción: `sim_routing_map` solo guarda `(iccid, provider, company_id, last_seen_at)`.

Esa restricción tiene un costo claro: **cualquier vista agregada cross-SIM requiere fan-out completo al provider**, lo que no escala a 134k SIMs distribuidas en Tele2/Moabits/Kite.

Posibles caminos:

**Camino A — Iterar credenciales en endpoint admin (sin cache).** El admin invoca un endpoint nuevo `/v1/admin/sims*` que itera por cada `(company_id, provider)` activa, paraleliza fan-out, agrega resultados. Funciona para Kite/Tele2; para Moabits usa la credencial parent (Bismark) — una llamada cubre todo. Mantiene ADR-002 intacto. Lento (~minutos para N tenants × 2 providers) y cada refresh cuesta lo mismo.

**Camino B — Cache materializada en routing map (esta ADR).** Extender `sim_routing_map` con `status`, `last_lu_at`, `services`, `operator`, mantenidos por el sync worker (ADR-012). KPIs y filtros server-side leen de DB local → milisegundos, independientes del provider. Stale por la cadencia del sync (24h por defecto, configurable). Rompe parcialmente ADR-002 pero sin afectar el path de detalle (que sigue siendo live).

**Camino C — Tabla separada `sim_status_snapshot`.** Misma idea que (B) pero en una tabla aparte, dejando `sim_routing_map` minimal. Más limpio conceptualmente; un poco más caro de mantener (otro modelo, otra migración, joins extra).

## Decisión propuesta

Adoptar **Camino C**: tabla nueva `sim_status_snapshot` mantenida por el sync worker, **sin tocar `sim_routing_map`**. Esto preserva ADR-002 estrictamente (`sim_routing_map` sigue siendo solo identidad) y aísla los campos cacheados a una tabla con vida útil explícita.

### Schema

```
sim_status_snapshot
  ├── iccid             TEXT      PK + FK(sim_routing_map.iccid)
  ├── provider          TEXT      NOT NULL  -- denormalized for index efficiency
  ├── company_id        UUID      NOT NULL  -- denormalized; index target
  ├── status            TEXT      NOT NULL  -- native provider value (ACTIVE/ACTIVATED/Active/...)
  ├── status_group      TEXT      NOT NULL  -- canonical group (active_like/...)
  ├── last_lu_at        TIMESTAMPTZ NULL
  ├── last_cdr_at       TIMESTAMPTZ NULL
  ├── operator          TEXT      NULL
  ├── country           TEXT      NULL
  ├── data_service      BOOLEAN   NULL
  ├── sms_service       BOOLEAN   NULL
  ├── refreshed_at      TIMESTAMPTZ NOT NULL  -- when this row was last updated by the worker
  └── source_run_id     UUID      NULL  -- sync_jobs.id that produced this row

  INDEX (provider, status)
  INDEX (company_id, provider)
  INDEX (last_lu_at)  -- for "sin LU reciente" queries
```

No incluye: msisdn, imsi, imei, plan/customer/limits. Esos siguen siendo live-only (path de detalle) o derivables de `provider_fields` cuando el listing los traiga.

### Mantenimiento

- El sync worker de ADR-012 hace `INSERT ... ON CONFLICT (iccid) DO UPDATE` con los campos cacheables al iterar el listing del provider.
- `refreshed_at` permite descartar rows obsoletas (> N días) en queries agregadas.
- Una migración inicial popula la tabla; runs subsiguientes la mantienen.

### Stale-data policy

- KPIs (`/sims/stats`), listado paginado global y filtros agregados leen de `sim_status_snapshot` cuando el caller declara `as_of=cache` (o por default).
- El path de detalle (`/sims/{iccid}`) **sigue siendo live** — nunca lee de la cache.
- Cualquier writeback de status (`PUT /sims/{iccid}/status`) invalida (`UPDATE refreshed_at = NULL`) el row, forzando refresh en el siguiente sync.
- La UI expone "actualizado hace N minutos/horas" basado en `refreshed_at` mínimo del scope consultado.

### RBAC y scope

Nuevo helper `get_current_scope(profile) -> ScopeSelector`:

```python
@dataclass
class ScopeSelector:
    company_ids: list[uuid.UUID] | None  # None = todas (admin global only)
    requested_provider: Provider | None
```

- `admin` → `company_ids=None` por default (vista global); puede acotarse vía `?company_id=...`
- `manager` / `member` → `company_ids=[profile.company_id]`

Los endpoints `/sims/*` y `/sims/stats` aceptan opcionalmente `?company_id=...` (validado contra el scope) y construyen el `WHERE` adecuado en `sim_status_snapshot`. El admin que filtra por `operator=Claro` recibe en una sola query (sin fan-out) los counts de todos los tenants.

### Endpoints

Cambios minimos vs los existentes:

| Endpoint | Cambio |
|---|---|
| `GET /v1/sims/stats` | Acepta `scope=cache\|live`. Default: `cache` para admin global, `live` para tenant-scoped. |
| `GET /v1/sims` | Mismo `scope`. Cuando `cache`, el listado paginado es ordenado por `sim_status_snapshot` con join contra `sim_routing_map` para hidratación. Cada `SubscriptionOut` lleva `detail_level="summary"` y un campo nuevo `cache_age_seconds`. |
| `POST /v1/sims/search` | Mismo `scope`. Filtros server-side aplicados en SQL directamente. |
| `GET /v1/sims/{iccid}` | Sin cambios — siempre live. |

### Trade-offs

| Pro | Contra |
|---|---|
| KPIs admin cross-tenant en milisegundos sin paginar al provider | Cache stale entre runs del sync (mitigable bajando cadencia) |
| Filtros sobre toda la base sin fan-out | Migración + nueva tabla + cambio en sync worker |
| Independiente del routing map; preserva ADR-002 estrictamente | Sumar storage (~134k rows × ~150 bytes ≈ 20 MB, despreciable) |
| El sync worker ya itera todos los SIMs en su run actual — el upsert sale "gratis" | Writeback invalidation requiere coordinar con `PUT /status` |

### No hacer

- **No cachear** msisdn/imsi/imei/plan/customer/limits/usage/presence. Esos siguen live.
- **No tocar `sim_routing_map`**. Permanece minimal por ADR-002 y ADR-012.
- **No usar la cache para reads single-SIM**. El detalle siempre va al provider.
- **No exponer endpoints `/v1/admin/sims*` paralelos**. Se reutilizan los `/v1/sims*` con `ScopeSelector`.

## Plan de implementación (cuando se acepte)

1. **Migración** `sim_status_snapshot` (Alembic).
2. **`get_current_scope`** + reemplazo de `get_current_company_id` en endpoints de listado/stats/search.
3. **Sync worker** (ADR-012) hace upsert a `sim_status_snapshot` además del routing map.
4. **Stats endpoint** lee de DB local cuando `scope=cache`; mantiene path live como fallback.
5. **Listado y search** sumar parámetro `scope`; ramificar lectura.
6. **Frontend**: admin ve toggle "Vista global / Mi company"; selector de `cache_age_seconds` informativo.
7. **Tests**: scoping, stale invalidation, fallback live, RBAC enforcement.

## Estado actual sin esta ADR

La iteración previa de plataforma cumple Fase A del análisis original:

- Filtros generales y de servicios (Plan, Cliente, Operador, Servicios, Sin LU reciente) abiertos a admin/manager/member.
- Filtros provider-specific avanzados (customField, rate_plan, communication_plan, autorenewal, product_name) restringidos a admin.
- Todos los datos están scoped automáticamente por `company_id` del caller via `get_current_company_id`.
- `/v1/sims/stats` opera en modo live; reporta `partial=true` cuando agota `_STATS_MAX_PAGES=100`.

**Esta ADR queda como ruta a seguir** cuando el dolor de paginación live se vuelva inaceptable o cuando producto pida vista admin cross-tenant.
