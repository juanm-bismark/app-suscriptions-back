# ADR-009 — Estrategia de testing: pirámide + golden files de mappers + contract tests + fake providers

- **Estado**: Accepted
- **Fecha**: 2026-04-22
- **Decisores**: equipo backend
- **Relacionado**: ADR-003 (ACL), ADR-004 (errores)

## Contexto

La superficie de bug más alta es **el mapeo entre DTOs del proveedor y el modelo canónico**. Los proveedores cambian sus payloads sin previo aviso (campos nuevos, valores nuevos en enums, formato de fechas). Sin tests, la API empieza a devolver datos sutilmente incorrectos y nadie se entera hasta que un cliente reclama.

El repo no tiene suite de tests visible al momento del análisis. Esta es una de las primeras deudas a pagar al introducir el módulo de proveedores.

## Decisión

Pirámide de tests con énfasis particular en mappers. Stack: `pytest`, `pytest-asyncio`, `respx` (mocking httpx), `hypothesis` (property-based, opcional), `polyfactory` (factories).

### Capa 1 — Unit tests sobre **mappers** (golden files)

**Volumen objetivo**: ≥ 90 % cobertura de cada `mappers.py`.

Estructura:

```
app/providers/kite/
  mappers.py
  tests/
    fixtures/
      get_subscription_active.kite.json       # respuesta real capturada
      get_subscription_active.canonical.json  # salida esperada
      get_subscription_purged.kite.json
      get_subscription_purged.canonical.json
      get_usage_30days.kite.json
      get_usage_30days.canonical.json
    test_mappers.py
```

`test_mappers.py` carga ambos JSON, ejecuta el mapper, compara con `assert canonical == expected`. Un cambio del proveedor = falla determinista con diff visible.

**Reglas**:
- Cada path significativo del proveedor (estados, edge cases observados, campos faltantes) tiene su par de fixtures.
- Los fixtures son texto plano JSON, **versionados en git**.
- Para regenerar: script `make regen-fixtures-kite` que reescribe los `.canonical.json` desde el mapper actual y deja al humano revisar el diff antes de commitear (peligroso pero necesario para iterar rápido).

### Capa 2 — Contract tests sobre **adapters**

Cada `KiteAdapter`, `Tele2Adapter`, `MoabitsAdapter` se testea con:

1. **Cumplimiento del Protocol**: existe un `test_provider_contract.py` parametrizado por adapter que verifica:
   - Todos los métodos del `SubscriptionProvider` están implementados.
   - Tipos de retorno coinciden.
   - Errores levantados son siempre subclases de `DomainError` (ADR-004).

2. **Comportamiento de errores**:
   - Provider devuelve 401 → adapter levanta `ProviderAuthFailed`.
   - Provider devuelve 429 con `Retry-After` → adapter levanta `ProviderRateLimited(retry_after=...)`.
   - Provider devuelve 503 → adapter levanta `ProviderUnavailable`.
   - Provider devuelve cuerpo malformado → adapter levanta `ProviderProtocolError`.
   - Timeout → `ProviderUnavailable`.

   Implementado con `respx` interceptando las llamadas httpx.

3. **Operaciones no soportadas**: algunos adapters no implementan la operación de control canónica `purge` bajo ese nombre y, en su lugar, exponen variantes (p.ej. Tele2 expone cambio de `status: PURGED`, Moabits documenta `Edit Device Details {status: PURGED}` y el adapter usa una ruta dedicada, Kite `networkReset`). Las pruebas contract verifican que cuando una operación no está disponible el adapter levanta `UnsupportedOperation` y la API devuelve `409 UnsupportedOperation`.

### Capa 3 — Integration tests sobre **services** del dominio

Inyectar `FakeProvider` (in-memory) que cumple el `SubscriptionProvider` Protocol. Tests:

- `SubscriptionFetcher.get(iccid)` resuelve el provider correcto del `SimRoutingMap`.
- `SubscriptionSearchService` provider-scoped con filtros soportados/no soportados y global listing vía routing map; si una llamada de la página falla, la response es `partial=true, failed_providers=[...]`.
- Idempotency: dos POST con la misma `Idempotency-Key` invocan al provider sólo una vez.
- Caché TTL: dos GET concurrentes al mismo `iccid` invocan al provider sólo una vez.
- Circuit breaker: 5 fallos consecutivos abren el circuito; el 6º falla en < 10 ms sin invocar al provider.

DB real (Postgres en docker-compose) para `SimRoutingMap` y `audit_log`. **Sin mocks de DB.** [feedback explícito: las pruebas con mock de DB pasan, prod migra distinto.]

### Capa 4 — Component tests sobre **routers**

`fastapi.testclient.TestClient` con dependency overrides para inyectar `FakeProviderRegistry`. Tests:

+- `POST /v1/sims/{iccid}/purge` sin `Idempotency-Key` → 400.
+- `POST /v1/sims/{iccid}/purge` con rol `manager` → 403 + auditado.
- Errores se serializan a Problem Details con `Content-Type: application/problem+json`.

### Capa 5 — End-to-end (opcional, CI nightly)

Sólo si el proveedor ofrece sandbox confiable. Si Kite/Tele2/Moabits exponen ambiente staging:
- Tests nightly contra sandbox real, para detectar drift de contrato.
- Si fallan, abren issue automático; no rompen el deploy.

`[REQUIRES INPUT: ¿alguno de los 3 proveedores ofrece sandbox?]`

### Capa 6 — Property-based (opcional, alto ROI)

Para los mappers críticos (`map_subscription`, `map_usage`):

```python
@given(kite_subscription_payload())  # hypothesis strategy
def test_map_subscription_never_raises(payload):
    result = map_subscription(payload)  # no debe levantar excepción
    assert result.iccid                 # invariante mínimo
    assert result.status in AdministrativeStatus
```

Cubre payloads que el equipo nunca pensó pero el proveedor puede emitir.

### Métricas y gates de CI

- Cobertura mínima: 80 % global, 90 % en `mappers.py`, 100 % en `errors.py` y `dependencies.py`.
- Tests de mappers son **bloqueantes** del PR.
- Tests E2E son **non-bloqueantes** (informativos).
- Tiempo total de la suite (capas 1–4): < 60 s. Capa 5 corre fuera del PR.

## Consecuencias

**Positivas**
- Cualquier cambio sutil del proveedor = falla determinista en CI.
- El dominio se desarrolla con `FakeProvider` — feedback en milisegundos, sin red.
- Refactor del modelo canónico: si los golden files no cambian, la API expone lo mismo. Tranquilidad.
- Onboarding: un dev nuevo agrega un proveedor copiando `kite/` como template, y los contract tests le dicen qué le falta.

**Negativas / mitigaciones**
- Mantener fixtures requiere disciplina (refrescar cuando el proveedor evoluciona) → **mitigación**: script de regeneración + revisión obligatoria del diff.
- El test pyramid en sí cuesta tiempo de setup → **mitigación**: el primer adapter (Kite) define los patrones; los siguientes son copy-paste-ajustar.

## Alternativas consideradas

1. **E2E heavy contra sandbox real, poco unit**
   - Pros: alta confianza.
   - Contras: lento, flaky, depende de disponibilidad y rate limits del proveedor, no detecta bugs en mappers (los enmascara).
   - **Rechazada como estrategia primaria**.

2. **Sólo unit, sin contract tests**
   - Pros: rápido.
   - Contras: no detecta drift de implementación contra Protocol; no detecta cuando un adapter olvida traducir un error específico.
   - **Rechazada**.

3. **Snapshot testing genérico** (sin separar payload y canonical)
   - Pros: menos código.
   - Contras: el snapshot mezcla input y output → cuando falla, no se distingue si cambió el proveedor o la lógica.
   - **Rechazada** a favor de fixtures separados.

## Trade-offs explícitos

| Eje | Pirámide + golden files (elegido) | E2E heavy |
|---|---|---|
| Velocidad de feedback | < 60 s | minutos |
| Sensibilidad a cambios del proveedor | máxima en mappers | indirecta |
| Costo de mantenimiento | medio (fixtures) | alto (sandbox flaky) |
| Costo $ | nulo | depende de cuotas |

## Cuándo revisar

- Si un proveedor empieza a fallar contract tests todas las semanas, el problema no es el test — es el proveedor; renegociar SLA o agregar un wrapper más estricto.
- Si la suite supera 5 minutos, dividir en parallel jobs por contexto.
