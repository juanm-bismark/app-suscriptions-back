# ADR-003 — Anti-Corruption Layer + Provider Adapter + Provider Registry

- **Estado**: Accepted
- **Fecha**: 2026-04-22
- **Decisores**: equipo backend
- **Relacionado**: ADR-001 (modular monolith), ADR-004 (modelo de errores), ADR-009 (testing), ADR-010 (bootstrap explícito de `company_codes` Moabits)

## Contexto

Tres proveedores con vocabularios incompatibles modelan el mismo concepto:

| Concepto | Kite | Tele2 | Moabits |
|---|---|---|---|
| ID SIM | `icc` | `iccid` | `iccid` |
| Estado | `lifeCycleStatus` | `status` | `simStatus` |
| Consumo | `consumption*` anidado | `Get Device Usage` | `simUsage` |
| Reset red | `networkReset` | (no soportado) | (no soportado como operación técnica separada) |
| Baja / purge administrativo | (no soportado por `modifySubscription`) | `Edit Device {PURGED}` | `Edit Device Details {status: PURGED}` / ruta dedicada observada |

Si esos conceptos se filtran al dominio (`subscription_data.icc`, `device.iccid`, `simInfo.iccid` viviendo en distintos lugares), el código se vuelve un árbol de `if provider == "kite"`. El cuarto proveedor sería catastrófico.

## Decisión

Aplicar **Anti-Corruption Layer** (DDD) con tres componentes:

### 1. `SubscriptionProvider` — Protocol/interface canónica

```python
# app/providers/base.py
from typing import Protocol, runtime_checkable
from app.subscriptions.domain import (
    Subscription, UsageSnapshot, ConnectivityPresence,
    AdministrativeStatus,
)

@runtime_checkable
class SubscriptionProvider(Protocol):
    """Required core interface — every adapter must implement these."""
    async def get_subscription(self, iccid: str, credentials: dict) -> Subscription: ...
    async def get_usage(self, iccid: str, credentials: dict) -> UsageSnapshot: ...
    async def get_presence(self, iccid: str, credentials: dict) -> ConnectivityPresence: ...
    async def set_administrative_status(self, iccid, credentials, *, target, idempotency_key) -> None: ...
    async def purge(self, iccid, credentials, *, idempotency_key) -> None: ...
```

Operaciones no soportadas por un proveedor levantan `UnsupportedOperation` (canónica, ADR-004) — **no** silenciar, **no** simular.

#### Capability Protocol Pattern (opt-in)

Se aplica el patrón conocido como "Capability Protocol Pattern": las
capacidades opcionales de un proveedor se modelan como `Protocol`
pequeños y `runtime_checkable`. Cada adapter implementa únicamente los
métodos que realmente soporta. El router o service usa `isinstance`
para detectar la presencia de una capacidad y elegir el camino adecuado.

Beneficios:
- Evita stubs que hagan `raise UnsupportedOperation` en adaptadores que
    no soportan una operación.
- Mantiene el Interface Segregation Principle (ISP): las capacidades se
    definen por separado y el consumo explícito queda en el caller.
- Mejora la claridad en las pruebas: es trivial construir un `Fake` que
    implemente sólo las capacidades requeridas por el caso de uso.

Ejemplo (listado nativo del proveedor):

```python
@runtime_checkable
class SearchableProvider(Protocol):
        """Listado nativo del proveedor — el scope lo dan las credenciales."""
        async def list_subscriptions(
                self, credentials: dict, *,
                cursor: str | None, limit: int,
        ) -> tuple[list[Subscription], str | None]: ...
```

**Scope = credenciales, no parámetro.** Cada credencial almacenada es
`(company_id, provider)` y se resuelve contra el proveedor con un token
que ya identifica al tenant en su lado (cuenta Kite, API key Tele2,
`company_codes` Moabits). Por eso `list_subscriptions` **no recibe
`company_id`**: pasarlo sería redundante (el token ya lo restringe) y en
algunos casos engañoso (p. ej. filtrar Kite por `customField_1` además
de su token sólo serviría para sub-tenancy compartida — no es el caso del
MVP).

Pagination y rate limit son nativos del proveedor:

| Proveedor | Cursor nativo | Límite máximo por request | Notas |
|---|---|---|---|
| Kite | `startIndex` (offset) | `maxBatchSize` ≤ 1000 | Adapter clamp; 429 → `ProviderRateLimited`. |
| Tele2 | `pageNumber` (1-based) | `pageSize` ≤ 50 | `lastPage:true` cierra la paginación. El adapter enriquece sólo las primeras 5 filas con `Get Device Details`; el resto queda como summary. |
| Moabits | offset local | sin paginación nativa | `getSimListByCompany` devuelve todo; el adapter pagina en memoria. |

Hoy los tres adaptadores (Kite, Tele2, Moabits) implementan
`SearchableProvider`. Si en el futuro se incorpora un proveedor sin
listado nativo, simplemente no implementa el método y el router cae al
camino global basado en `SimRoutingMap` (ver más abajo).

#### Listing behaviour and tenant bootstrap (provider required by default)

Para evitar ambigüedades operativas y porque las credenciales son
siempre `Company × Provider` (no existe una credencial global que
permita listar de todos los proveedores para una tenant), la política
operativa para el MVP es **exigir siempre** `?provider=<name>` en
`GET /v1/sims`.

Razonamiento:
- Las credenciales para cada proveedor deben resolver con `company_id` y
    `provider` desde `company_provider_credentials` (tabla cifrada). No
    es viable ni seguro intentar un listado global sin conocer qué
    credenciales usar para cada llamada.
- El `SimRoutingMap` (iccid → provider) es una optimización útil, pero
    **requiere bootstrap por tenant** (CSV import o proceso administrativo)
    para poder paginar globalmente. Si no existe ese bootstrap, no hay
    forma fiable de presentar una vista global completa sin consultar a
    cada proveedor con credenciales específicas del tenant.

Comportamiento operativo recomendado:

- Provider-scoped listing (requerido): `GET /v1/sims?provider=<name>`
    — resolver credenciales `company_provider_credentials` y llamar al
    adapter; si el adapter implementa `SearchableProvider`, delegar la
    paginación nativa al proveedor.

    Para Tele2, el listing nativo (`Search Devices`) devuelve un resumen
    liviano. El adapter llama `Get Device Details` únicamente para las
    primeras 5 SIMs de la página, respetando el rate limiter de cuenta.
    El router serializa esas filas como `detail_level=detail`; las demás
    filas conservan `detail_level=summary`.

- Global listing (deshabilitado por defecto): sólo habilitado cuando el
    tenant tiene un `SimRoutingMap` inicializado (import admin o proceso
    de discovery controlado). Mientras no exista, el endpoint global debe
    devolver `400` o `412` indicando que el tenant no tiene routing map y
    que debe usar `?provider=`.

Pseudocódigo (provider-scoped — requerido):

```python
# Provider-scoped listing (default and recommended)
creds = resolve_creds(company_id, provider)  # already scoped by tenant
adapter = provider_registry.get(provider)
if isinstance(adapter, SearchableProvider):
        items, next_cursor = await adapter.list_subscriptions(creds, cursor=cursor, limit=limit)
else:
        # adapter no expone listado nativo → 412 o fallback admin-only
        raise HTTPException(status_code=412, detail="provider does not support company-scoped search")
```

Bootstrap note (global listing): documentar el proceso admin para importar
o construir `sim_routing_map` por tenant antes de habilitar la vista
global. Ver `migrations/001_sim_routing_map.sql` y
`migrations/002_company_provider_credentials.sql` para cómo se resuelve el
routing y se guardan las credenciales por tenant.

Bootstrap note (Moabits `company_codes`): el listado provider-scoped de
Moabits requiere `company_codes` persistidos en `credentials_enc` antes
del primer listado. Si el campo está vacío, el router responde
`412 ListingPreconditionFailed` apuntando al flujo
`GET /v1/companies/me/credentials/moabits/companies/discover` +
`PUT /v1/companies/me/credentials/moabits/company-codes`. No hay
auto-scope por nombre. Ver ADR-010.

### 2. **Provider Adapters** — implementación por proveedor

Cada `app/providers/<name>/` contiene:
- `client.py` — cliente HTTP (httpx) con auth específica del proveedor.
- `dto.py` — modelos Pydantic que reflejan **exactamente** el payload del proveedor.
- `mappers.py` — funciones puras `dto → modelo canónico`. **Acá vive todo el conocimiento del vocabulario del proveedor.**
- `adapter.py` — implementa `SubscriptionProvider`, orquesta cliente + mapper.

Los DTOs del proveedor **nunca cruzan** la frontera de `app/providers/<name>/`. Se quedan en el adapter.

### 3. **Provider Registry** — resolución por enum

```python
# app/providers/registry.py
class ProviderRegistry:
    def __init__(self, kite: KiteAdapter, tele2: Tele2Adapter, moabits: MoabitsAdapter): ...
    def get(self, provider: Provider) -> SubscriptionProvider: ...
```

Inyectado vía `Depends` en los services del dominio. Los routers nunca importan un adapter concreto.

### 4. Resolución de credenciales

Cada adapter recibe credenciales específicas-del-tenant en cada llamada (no se "instancia un adapter por tenant"). El service hace:

```python
creds = await credential_resolver.resolve(company_id, provider)
async with provider_registry.get(provider).with_credentials(creds) as p:
    return await p.get_subscription(iccid)
```

`with_credentials` devuelve un wrapper que inyecta auth headers/tokens en las llamadas de esa request. El adapter en sí permanece stateless y reusable.

## Consecuencias

**Positivas**
- **SOLID limpio**:
  - SRP: cada adapter, una sola fuente.
  - OCP: agregar Provider #4 = nuevo paquete, cero cambios en `subscriptions/` ni routers.
  - LSP: todos los adapters cumplen el mismo Protocol.
  - ISP: el Protocol es chico; si un proveedor expone más cosas (ej. `search_by_imei`), va en una interfaz separada (`SupportsImeiSearch`) y el service hace `isinstance`.
  - DIP: services dependen del Protocol, nunca del adapter.
- Testabilidad altísima: el dominio se testea con un `FakeProvider` in-memory; cada adapter se testea con golden files (ADR-009).
- Una **frontera explícita** entre dominio canónico y vocabulario externo. La code review puede defender la frontera.

**Negativas / mitigaciones**
- Dos modelos por concepto (DTO + canónico) → más código → **mitigación**: los DTOs son Pydantic puro, generados desde el OpenAPI/WSDL del proveedor cuando sea posible.
- El mapper es donde se concentra el riesgo de bugs sutiles → **mitigación**: golden files versionados + property-based tests donde aplique (ADR-009).

## Alternativas consideradas

1. **Funciones de traducción ad-hoc** (sin Protocol, sin clase Adapter)
   - Pros: menos boilerplate.
   - Contras: imposible inyectar/mockear; ramas `if provider==` se filtran a routers.
   - **Rechazada**.

2. **Modelo unificado expuesto por una librería externa** tipo "telco-iot-provider-sdk"
   - No existe ninguna que cubra Kite + Tele2 + Moabits.
   - **N/A**.

3. **Code generation desde OpenAPI** del proveedor sin capa canónica
   - Pros: cliente generado.
   - Contras: el cliente queda con el vocabulario del proveedor; sigue habiendo `if provider==` en el dominio.
   - **Rechazada como sustituto** (sí se puede usar para los DTOs, pero el ACL sigue siendo necesario).

## Trade-offs explícitos

| Eje | ACL + Adapter (elegido) | Sin ACL |
|---|---|---|
| Costo de N+1 proveedor | un paquete nuevo | refactor del dominio |
| Acoplamiento | bajo (sólo Protocol) | alto (vocabulario filtrado) |
| Tests del dominio | con FakeProvider, microsegundos | requieren mockear cada proveedor |
| Líneas de código | más | menos |
| Mantenibilidad a 12 meses | alta | baja |

## Cuándo revisar

- Si **todos** los proveedores convergen en un mismo modelo de datos (ej. estándar telco), el ACL puede colapsar a un cliente HTTP genérico. Improbable.
- Si la app deja de ser proxy y pasa a agregador (ADR-002 invalidado), los adapters siguen, pero aparece una capa de "syncher" arriba.
