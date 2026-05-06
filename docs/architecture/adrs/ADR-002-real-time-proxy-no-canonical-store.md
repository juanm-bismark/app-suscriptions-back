# ADR-002 — Proxy en tiempo real: sin almacén canónico de SIMs

- **Estado**: Accepted
- **Fecha**: 2026-04-22
- **Decisores**: equipo backend
- **Relacionado**: ADR-005 (resiliencia + caché TTL ≤ 5 s)

## Contexto

Requisito explícito del producto: la API **no** mantiene una copia local del catálogo de SIMs. Cada consulta del cliente se traduce a una o más llamadas a los proveedores y la respuesta se serializa al modelo canónico. Sin batch jobs, sin sincronización nocturna, sin proceso de reconciliación.

Aun así, la API necesita resolver **a qué proveedor pertenece un `iccid`** sin tener que llamar a los tres por cada request — eso multiplicaría latencia y consumo de cuota.

## Decisión

1. **Cero persistencia del estado de SIM.** No existe tabla `subscriptions`, `usages`, `presence`, ni similar. Todo lo que el cliente vea sobre una SIM proviene de una llamada en vivo al proveedor (o de un de-duplicador in-memory con TTL ≤ 5 s — ver ADR-005).
2. **Sí persistir un `SIM Routing Map`** — tabla `sim_routing_map(iccid PK, provider, company_id, last_seen_at)` cuya única responsabilidad es resolver `iccid → provider, company`. Es ruteo, no espejo del dominio.
3. La frescura del dato es un detalle operacional del adapter y no un campo público estable del modelo canónico.

### Carga inicial del routing map

Modos soportados, en orden de preferencia:

- **CSV bootstrap** — el equipo importa un dump del proveedor con `iccid, provider, company_id`. Es la opción operacionalmente sana.
- **Provider-scoped discovery** — una llamada `GET /v1/sims?provider=<name>` usa el listing nativo del adapter y actualiza el routing map con las SIMs observadas.
- **Import manual** — `POST /v1/sims/import` permite poblar `iccid/provider` cuando el dump inicial viene de otro proceso.

No se implementa lazy discovery por fan-out cross-provider en v1. Si una request por `iccid` llega sin entrada en `sim_routing_map`, el backend responde `404 SubscriptionNotFound` o pide bootstrap/listing previo, según el endpoint.

## Consecuencias

**Positivas**
- **Cero divergencia entre dato local y proveedor**: la verdad siempre es el proveedor. Imposible ver datos rancios por bug de sync.
- Cero infra de pipelines, cero deuda de jobs, cero alertas de "sync atrasado".
- Modelo de seguridad simple: la API no tiene catálogo de PII de SIMs en su DB.

**Negativas / mitigaciones**
- La latencia de cada request está acotada por el proveedor → **mitigación**: timeouts agresivos + circuit breaker (ADR-005).
- Cuando un proveedor cae, las consultas a SIMs de ese proveedor fallan → **mitigación**: respuesta canónica `503 ProviderUnavailable` rápida (no el timeout completo); las SIMs de otros proveedores siguen funcionando.
- No hay reportería offline ni búsquedas cross-tenant globales → si el producto las pide, esto se invalida.
- Cada GET cuesta una request al proveedor → **mitigación**: caché TTL ≤ 5 s para de-duplicar concurrentes; throttling por `company_id`.

## Alternativas consideradas

1. **Modo agregador** (replicar todo en Postgres + sync periódico)
   - Pros: queries rápidas locales; tolerancia a caída del proveedor; reportería trivial.
   - Contras: arquitectura completamente distinta (event-driven o polling), consistencia eventual, manejo de divergencia, costo de pipelines, riesgo de servir dato rancio. Explícitamente rechazado por el producto.
   - **Rechazada**.

2. **Sin routing map — fan-out a los 3 proveedores en cada request por `iccid`**
   - Pros: cero estado.
   - Contras: triplica latencia y cuota; complica el manejo de errores parciales; no tiene sentido cuando el `iccid` realmente vive en exactamente un proveedor.
   - **Rechazada**.

3. **Routing por header del cliente** (el cliente le dice al servidor qué proveedor usar)
   - Pros: cero estado en servidor.
   - Contras: filtra concepto de "proveedor" al cliente y al usuario final; ata el frontend al modelo interno; propenso a errores del cliente.
   - **Rechazada**.

## Trade-offs explícitos

| Eje | Proxy puro (elegido) | Agregador |
|---|---|---|
| Frescura del dato | máxima (en vivo) | dependiente del sync |
| Latencia | media-alta (= proveedor) | baja (Postgres local) |
| Tolerancia a caída del proveedor | nula para SIMs de ese proveedor | alta (datos cacheados) |
| Consistencia | trivial | eventual + complejidad |
| Costo operativo | mínimo | alto (jobs, monitoring de drift) |
| Reportería offline | imposible | nativa |

## Cuándo revisar

- El producto pide reportes históricos cross-proveedor o búsquedas full-text < 100 ms sobre 134k SIMs.
- Las cuotas/costos del proveedor se vuelven incompatibles con "una request por consulta".
- Se introduce un caso de uso offline (el cliente debe operar cuando el proveedor está caído).

→ Cualquiera de estos invalida ADR-002 y obliga a pasar a modo agregador, lo que implica redibujar Phase 2 (aggregates con persistencia propia) y Phase 3 (event-driven sync).
