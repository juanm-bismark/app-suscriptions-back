# ADR-005 — Resiliencia: timeout, retry, circuit breaker, caché TTL ≤ 5s

- **Estado**: Accepted
- **Fecha**: 2026-04-22
- **Decisores**: equipo backend
- **Relacionado**: ADR-002 (proxy puro), ADR-003 (ACL), ADR-004 (errores)

## Contexto

La API depende totalmente de proveedores externos. Sin políticas explícitas:
- Un proveedor lento bloquea el event loop de FastAPI.
- Un flap de red causa errores que un retry simple resolvería.
- Stampedes de requests concurrentes al mismo `iccid` saturan al proveedor por nada.
- Una caída prolongada del proveedor degrada todo el servicio en vez de fallar rápido.

## Decisión

Cuatro mecanismos combinados, configurables por adapter:

### Estado de implementación al 2026-05-06

| Mecanismo | Estado | Nota |
|---|---|---|
| Circuit breaker por adapter | Implementado | `BaseAdapter` usa `CircuitBreaker` en memoria por proceso. |
| Tele2/Jasper fair-use limiter | Implementado | Serializa llamadas Tele2 por cuenta en proceso y respeta `max_tps` (default 1, Advantage típico 5). |
| Cisco `40000029` / HTTP 429 | Implementado | Ambos se traducen a `ProviderRateLimited`; el limiter aumenta backoff temporal. |
| Timeouts por adapter | Parcial | Los adapters usan timeouts `httpx`; Moabits v2 detail/connectivity tiene settings dedicados, pero no todos los timeouts v1/Kite/Tele2 están externalizados. |
| Retry con backoff idempotente | No aplicado | Evitado por ahora para no amplificar cuotas/TPS sin métricas reales. |
| Caché L1 TTL + single-flight | No aplicado | Documentado como siguiente paso; no hay cache anti-stampede todavía. |
| Semáforo genérico por adapter | No aplicado | Tele2 tiene limiter específico; Moabits v2 enrichment tiene semáforo por chunks; Kite/Moabits v1 no tienen bulkhead genérico aún. |
| Métricas Prometheus | No aplicado | No existe `/metrics` ni exportador de estado del circuit breaker todavía. |
| Redis/shared limiter | No aplicado | Requerido antes de múltiples workers/containers con TPS estricto. |

### 1. Timeouts duros en `httpx.AsyncClient`

```python
httpx.Timeout(connect=2.0, read=8.0, write=2.0, pool=2.0)
```

Valores por adapter en `Settings` (env-overridable). El read timeout es el más sensible y se calibra contra el P99 observado de cada proveedor.

### 2. Retry con backoff exponencial (sólo idempotentes)

- Sólo en métodos `GET` y en errores reintentables: `httpx.TimeoutException`, `httpx.ConnectError`, status 502/503/504.
- Política objetivo: 3 intentos máximo, backoff `0.2s, 0.6s, 1.8s` con jitter ±25%.
- Implementación pendiente: `tenacity` o decorador propio chico. **NO** retry en mutaciones (operaciones de control canónicas: `purge`) hasta confirmar idempotencia explícita del proveedor. La API implementada expone `POST /v1/sims/{iccid}/purge`; los adapters mapean la operación canónica al endpoint proveedor apropiado.

### 3. Circuit breaker por adapter

- Estados: `closed → open → half_open → closed`.
- Apertura: ≥ 5 fallos en ventana de 30 s, o ≥ 50% de fallos sobre ≥ 20 requests.
- Tiempo en `open`: 30 s. Luego `half_open` con 1 sonda. Si OK, vuelve a `closed`.
- Estado en memoria (proceso único). Métrica Prometheus pendiente.
- Implementación actual: `app/shared/resilience.py::CircuitBreaker`.
- Cuando está abierto: la request del cliente recibe `503 ProviderUnavailable` en < 10 ms.

### 4. Caché L1 in-memory con TTL ≤ 5 s (anti-stampede)

- Caché por adapter, **clave = `(operation, iccid, frozenset(params))`**.
- TTL = 5 s para `get_subscription`, `get_status_detail`, `get_presence`. **0** (deshabilitado) para `get_usage` — `[REVISED in Phase 7: NFR-C1 + semántica de consumo exigen frescura; cachear consumo sería mentir sobre facturación].`
- `cachetools.TTLCache` o `aiocache` con backend memory.
- **Single-flight**: requests concurrentes con la misma clave esperan al mismo future, no disparan llamadas duplicadas.
- La API no expone un `fetched_at` público en `Subscription`; la frescura es un detalle operacional del adapter y no parte del contrato.

### 5. Concurrencia limitada por adapter

`asyncio.Semaphore(N)` por adapter, default `N=20`. Evita que un proveedor lento agote el pool de conexiones disponible para los otros dos.

**Revisión Tele2/Jasper**: para Tele2 no se usa el semáforo genérico anterior. Se usa un limiter más estricto por política Cisco:
- una llamada Tele2 a la vez por cuenta/tenant dentro del proceso;
- intervalo mínimo derivado de `max_tps` (`1 / max_tps`);
- `max_tps` default `1`;
- `max_tps=5` para cuentas Advantage si se configura en `account_scope`;
- backoff incremental cuando Jasper devuelve `40000029 Rate Limit Exceeded` o HTTP 429.

Este mecanismo protege un solo proceso. Si se ejecutan varios workers o réplicas, el presupuesto TPS se multiplica accidentalmente y debe migrarse a Redis o a un rate limiter distribuido.

### 6. Listados con resultados parciales

`SubscriptionSearchService` evita fan-out cross-provider por defecto. Para `?provider=<name>`, delega al listing nativo del adapter. Sin `provider`, pagina sobre `sim_routing_map` y consulta el proveedor ya resuelto para cada ICCID de la página. Si la página puede devolver datos útiles pero alguna llamada falla, devolver `Page{partial=true, failed_providers=[...]}` (definido en ADR-004).

## Consecuencias

**Positivas**
- Latencia acotada: peor caso = `read_timeout` (≤ 8 s) por proveedor, **no** segundos sin techo.
- Una caída de proveedor abre el circuito en < 30 s y deja de propagar latencia.
- Cuando se implemente el caché single-flight, un stampede de 50 requests concurrentes al mismo `iccid` debería producir 1 sola llamada al proveedor. Hoy esto sigue pendiente salvo limitadores específicos como Tele2.
- Cuota del proveedor parcialmente protegida: Tele2 tiene limiter específico; el resto depende de timeouts/circuit breaker y de futuros bulkheads/caches.

**Negativas / mitigaciones**
- Caché in-memory no se comparte entre workers de uvicorn → duplicación parcial si hay >1 worker → **mitigación**: aceptable a 15–20 concurrentes; si pesa, mover a Redis con `aiocache(redis)`.
- Circuit breaker es per-process → **mitigación**: igual; un breaker abierto en un worker no afecta a otro, lo que de hecho da degradación gradual. Si pesa, breaker compartido vía Redis.
- Cuando exista caché TTL, las mutaciones deberán invalidar la entrada del `iccid` afectado. Hoy no hay caché genérico que invalidar.

## Alternativas consideradas

1. **Sin retries**: cualquier flap del proveedor falla. Mala UX.
2. **Retry agresivo (10 intentos)**: amplifica caídas, incumple cuotas, viola cualquier rate limit.
3. **Sin circuit breaker**: una caída de 10 minutos del proveedor = 10 min de timeouts encadenados.
4. **Caché con TTL largo (60 s+)**: rompe la promesa de proxy puro. Para consumos sería *mentir*.
5. **Bulkhead con thread pools** (en vez de semáforo asyncio): innecesario en stack 100% async.

## Trade-offs explícitos

| Mecanismo | Costo | Beneficio | Si se omite |
|---|---|---|---|
| Timeout | trivial | latencia acotada | requests colgados indefinidamente |
| Retry idempotente | bajo | enmascara flaps transitorios | UX errática |
| Circuit breaker | bajo | falla rápido bajo caída | latencia se compone |
| Caché TTL ≤5s | bajo | -90% requests al pico | stampede al proveedor |
| Semáforo por adapter | trivial | aislamiento entre proveedores | cascada de bloqueos |

## Cuándo revisar

- Si se llega a >1 worker o se escala horizontalmente, mover circuit breaker y caché a Redis.
- Si los proveedores garantizan idempotencia explícita en mutaciones, habilitar retry de mutaciones también.
