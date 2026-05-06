# Phase 2 — Modelo de Dominio

> **Principio**: el modelo expresa el **negocio** (gestión de SIMs/suscripciones M2M/IoT), no la suma de los APIs de Kite, Tele2 y Moabits. Cada proveedor expone vocabulario propio; este modelo lo absorbe en uno solo.

---

## 1. Bounded Contexts

| Bounded Context | Responsabilidad | Owner | Persistencia |
|---|---|---|---|
| **Identity & Access** | Autenticación, sesiones, roles. *(existente)* | Backend | Postgres (`users`, `profiles`, `refresh_tokens`) |
| **Tenancy** | Compañías cliente, configuración de tenant, vínculo a credenciales de proveedor. *(parcialmente existente — falta `company_provider_credentials`)* | Backend | Postgres (`companies`, `company_settings`, `company_provider_credentials`) |
| **Subscription Aggregation** | Modelo canónico de suscripción/SIM, agregación read-only en tiempo real desde proveedores, operaciones de control (purge). *(nuevo)* | Backend | Postgres sólo para enrutamiento (`sim_routing_map`) — **NO** persiste estado del SIM |
| **Provider Integration** | Adapters HTTP por proveedor (Kite, Tele2, Moabits). Anti-corruption layer. *(nuevo)* | Backend | sin estado propio (clients HTTP + cache TTL en memoria/Redis opcional) |

> Tres contextos de dominio + un contexto técnico (Provider Integration). El último existe porque sin un anti-corruption layer explícito, la traducción se filtra al dominio.

---

## 2. Lenguaje Ubicuo (glosario)

Esta sección es **normativa**: cuando aparezca uno de estos términos en código, ADRs, diagramas o conversaciones, significa exactamente lo que dice acá. Vocabulario de proveedor (icc/iccid, lifeCycleStatus, simStatus, …) **sólo puede aparecer dentro de un Provider Adapter**.

| Término canónico | Definición | Equivalentes en proveedores |
|---|---|---|
| **Subscription** | Una línea M2M/IoT identificada por su `iccid`. Aggregate root del contexto Subscription Aggregation. | Kite `subscriptionData` / Tele2 `device` / Moabits `simInfo` |
| **iccid** | Identificador físico único de la SIM. **Es la clave canónica del sistema.** | Kite `icc` / Tele2 `iccid` / Moabits `iccid` |
| **imsi**, **msisdn**, **imei** | Identificadores secundarios. Se exponen pero no se usan como clave primaria. | iguales en los tres |
| **AdministrativeStatus** | Estado del ciclo de vida administrativo: `ACTIVE`, `TEST`, `SUSPENDED`, `DEACTIVATED`, `PURGED`, `UNKNOWN`. | Kite `lifeCycleStatus` (ACTIVE/TEST/DEACTIVATED) · Tele2 `status` (ACTIVATED/PURGED) · Moabits `simStatus` (Active/Ready/Suspended) |
| **ConnectivityPresence** | Presencia técnica en la red en un instante: `level` (UNKNOWN/GSM/GPRS/IP), `online: bool`, `country`, `network`, `rat`, `rat_type`, `ip`, `apn`, `timestamp`. | Kite `presenceDetailData` · Tele2 (no expone) · Moabits `connectivityStatus` |
| **UsageSnapshot** | Consumo agregado en una ventana de tiempo: totales conveniencia `voice_seconds`, `sms_count`, `data_used_bytes`, más `provider_metrics` (bloque crudo del proveedor) y `usage_metrics[]` (lista normalizada de métricas). | Kite `consumptionDaily/Monthly` · Tele2 `Get Device Usage` · Moabits `getSimUsage` |
| **UsageMetric** | Métrica individual normalizada de consumo. Campos típicos: `name`, `value`, `unit`, `kind` y opcionalmente `period_start`/`period_end`. | Normaliza las métricas específicas de cada provider hacia un formato estable. |
| **ConsumptionLimit** | Límite configurado y comportamiento al excederse: `value`, `unit`, `enabled`, `trafficCut: bool`. | Kite anidado en `consumption*` · Tele2 `overageLimitOverride` · Moabits `dataLimit/smsLimit` |
| **CommercialPlan** | Plan/tarifa asignado: `code`, `name`, `start_date`, `end_date`. | Kite `commercialGroup` · Tele2 `ratePlan` + `communicationPlan` · Moabits `product_*` + `planStartDate/planExpirationDate` |
| **StatusChange** | Evento histórico de cambio de estado: `from_state`, `to_state`, `at`, `automatic: bool`, `reason`, `actor`. Sólo Kite expone histórico nativo; los demás generarán este evento sintético si se sincroniza alguna vez. | Kite `getStatusHistory` |
| **ControlOperation (Purge)** | Operación canónica de control sobre una SIM que el sistema expone para acciones administrativas o de red. En la práctica un único comando canónico (nombrado `purge` en el dominio) se mapea a distintas APIs proveedoras que por motivos históricos usan verbos diferentes (`networkReset`, `Edit Device {status: PURGED}`, rutas dedicadas de purga). Parámetros típicos: `iccid`, `technologies?`, `idempotency_key?`. | Kite `networkReset(network2g3g, network4g)` · Tele2 `Edit Device Details {status: PURGED}` · Moabits `PUT /api/sim/purge/` |
| **Provider** | Origen externo de los datos. Enum: `KITE`, `TELE2`, `MOABITS`. Toda Subscription tiene exactamente un Provider asignado. | n/a (es del modelo canónico) |
| **SubscriptionId (canónico)** | `{provider, iccid}` — el `iccid` por sí solo es único en la práctica, pero el par evita ambigüedad si dos proveedores ven la misma SIM (caso límite). | n/a |

---

## 3. Aggregates y Entities

### Subscription (Aggregate Root)

```
Subscription {
  iccid: ICCID                          # identidad
  provider: Provider                    # KITE | TELE2 | MOABITS
  company_id: CompanyId                 # tenant owner (vía SIM Routing Map)
  msisdn?: MSISDN
  imsi?: IMSI
  status: AdministrativeStatus
  native_status?: string
  provider_fields: Map<string, string>   # campos extensibles del proveedor (mapeados pero no interpretados)
  activated_at?: timestamp
  updated_at?: timestamp
}
```

**Reglas de invariante**:
- `iccid` es inmutable y obligatorio.
- `provider` es inmutable. Cambiar de proveedor = baja + alta, no `update`.
- `provider_fields` concentra el vocabulario específico del proveedor; el modelo canónico no interpreta claves fuera de esta bolsa.

### StatusChange (Entity dentro de la History)

```
StatusChange {
  iccid: ICCID
  from_state: AdministrativeStatus
  to_state: AdministrativeStatus
  at: timestamp
  automatic: bool
  reason: string
  actor?: string                        # null si automatic=true
}
```

> Sólo Kite lo expone hoy. Tele2 y Moabits deben devolver `not_supported` para esta capacidad hasta que exista endpoint equivalente documentado.

### CompanyProviderCredentials (Tenancy context)

```
CompanyProviderCredentials {
  company_id: CompanyId                 # FK a companies
  provider: Provider
  credentials: EncryptedBlob            # cifrado con Fernet/FERNET_KEY — NO en plano
  account_scope: {                      # qué cuenta/grupo del proveedor representa esta credencial
    kite?: { end_customer_id: string, cert_expires_at?: date, environment?: string }
    tele2?: { account_id: string }
    moabits?: { company_codes: [string] }
  }
  active: bool
  rotated_at: timestamp
}
```

> Tabla nueva. **NO** reutiliza `company_settings.settings` JSONB (ver Phase 1 AP-7).

### SimRoutingMap (Subscription Aggregation context)

```
SimRoutingMap {
  iccid: ICCID                          # PK
  provider: Provider
  company_id: CompanyId
  last_seen_at: timestamp               # actualizado cuando una request resuelve este iccid
}
```

> **Es la única tabla persistente del contexto Subscription Aggregation.** Resuelve la pregunta "¿a qué proveedor le pregunto por este ICCID?" sin tener que hacer fan-out a los 3. No persiste el estado de la SIM — es ruteo, no espejo.

---

## 4. Domain Services

| Servicio | Responsabilidad | Pertenece a |
|---|---|---|
| `SubscriptionFetcher` | Dado un `iccid`, resolver `provider` vía `SimRoutingMap`, invocar el adapter correspondiente, devolver `Subscription` canónica. Sin caché aquí — caché es decisión del adapter. | Subscription Aggregation |
| `SubscriptionSearchService` | Dado un criterio de búsqueda + `company_id`, ejecutar listing provider-scoped cuando exista `?provider`, o paginar `sim_routing_map` para la vista global. Aplica filtros sólo cuando el adapter los soporta documentalmente. | Subscription Aggregation |
| `SubscriptionOperationService` | Dado un `iccid` + comando (`ControlOperation`), resolver provider, invocar comando idempotente, devolver resultado canónico. Verifica RBAC (matriz en ADR-008) y escribe `audit_log`. | Subscription Aggregation |
| `ProviderRegistry` | Resuelve `Provider → SubscriptionProvider` (instancia de adapter inyectada vía `Depends`). | Provider Integration |
| `CredentialResolver` | Dado `(company_id, provider)`, devuelve credencial desencriptada con caché TTL corta. | Tenancy |

---

## 5. Relaciones entre contextos (modelo conceptual)

- **Identity & Access** → upstream de **Tenancy**: `Profile.company_id` apunta a `Company`. Sin cambios.
- **Tenancy** → upstream de **Subscription Aggregation**: cada operación se ejecuta en el scope de un `Company` (resuelto del `Profile` autenticado). `SimRoutingMap.company_id` y `CompanyProviderCredentials` viven aquí.
- **Subscription Aggregation** → downstream de **Provider Integration** vía **Anti-Corruption Layer**: el dominio no conoce los DTOs de proveedor. Cada adapter es un *Conformist* hacia su proveedor (no podemos cambiar sus contratos) y un traductor hacia nuestro modelo canónico.

Ver `context-map.mermaid`.

---

## 6. Decisiones semánticas críticas (no obvias)

### 6.1 Unificación de la operación de control (purge)
En la práctica operativa del producto hemos observado que lo que los proveedores llaman `purge`, `networkReset`, `Edit Device Details {status: PURGED}` o rutas dedicadas de purga representan la misma clase de operación de control sobre la SIM (forzar re-registro, cancelar localización, o marcar como purgada según el proveedor). Para evitar ambigüedad el modelo canónico expone una única operación de control llamada `purge` y obliga a los adapters a mapearla a la API propietaria del proveedor.

**Decisión**: el modelo canónico define una única `ControlOperation` con valor `purge`. Los adapters mapean según su semántica y capacidades:
- Adapter Kite: `purge` → `networkReset()`.
- Adapter Tele2: `purge` → `Edit Device Details {status: PURGED}` (semántica de baja administrativa en Tele2).
- Adapter Moabits: `purge` → Orion API 2.0.0 `PUT /api/sim/purge/` con body `{"iccidList": [iccid]}` y confirmación `info.purged=true`.

Si un proveedor no soporta la operación solicitada, el adapter debe levantar `UnsupportedOperation` y el router devolverá `409 UnsupportedOperation` con detalles del provider y la operación. Esta unificación reduce la complejidad del cliente y aclara la intención semántica del comando canónico.

La API implementada expone esta operación como `POST /v1/sims/{iccid}/purge`. No se adoptó el estilo `custom verb` de AIP-136 en esta versión del backend.

### 6.2 Estados administrativos: mapeo no-biyectivo
| Canonical | Kite | Tele2 | Moabits |
|---|---|---|---|
| ACTIVE | ACTIVE | ACTIVATED | Active |
| TEST / IN_TEST | TEST | TEST_READY | Ready / TEST_READY |
| SUSPENDED | SUSPENDED | (no valor Cisco documentado) | Suspended |
| DEACTIVATED / TERMINATED | DEACTIVATED | DEACTIVATED / RETIRED | DEACTIVATED |
| PURGED | (no lifecycle status Kite documentado) | PURGED | PURGED / purge route |
| INVENTORY | (no equivalente Kite core) | INVENTORY | INVENTORY |
| REPLACED | (no equivalente Kite core) | REPLACED | (no confirmado) |
| UNKNOWN | fallback | fallback | fallback |

Cuando el proveedor devuelva un valor fuera de su columna, el adapter mapea a `UNKNOWN` y emite log estructurado con el valor original. **No fallar la request por un estado nuevo del proveedor.**

### 6.3 Frescura del dato (proxy puro)
El cliente sabe que está consultando datos en tiempo real a través del proveedor, pero la API no expone una marca global de frescura en `Subscription`. El adapter puede aplicar caché in-memory con TTL ≤ 5 s para deduplicar requests concurrentes al mismo `iccid` (stampede prevention); en esa capa la frescura se mantiene como detalle operacional, no como parte del contrato público.

### 6.4 Búsquedas canónicas
`GET /v1/sims` tiene dos caminos explícitos:

1. **Provider-scoped listing** (`?provider=kite|tele2|moabits`): delega al listing nativo del adapter, aplica sólo filtros que el proveedor pueda mapear documentalmente y actualiza `sim_routing_map` con las SIMs observadas.
2. **Global listing** (sin `provider`): pagina sobre `sim_routing_map` y consulta el proveedor ya conocido para cada ICCID de la página. No descubre proveedores por fan-out.

Los filtros explícitos (`status`, fechas, `iccid`, `imsi`, `msisdn`, `custom`) requieren el camino provider-scoped hasta que exista una semántica cross-provider confirmada. Si el proveedor no soporta un filtro, la API devuelve `409 provider.unsupported_operation`.

Si una página global puede devolver datos útiles pero alguna llamada a proveedor falla, la respuesta conserva `200` y agrega `partial: true` con `failed_providers[]`. El cliente no recibe datos silenciosamente incompletos.

---

## 7. Registro en `component_names`

Confirmados en `_context_state.json` y vinculantes para todos los documentos siguientes:
- `Subscriptions API` (servicio raíz)
- `Subscription` (aggregate root)
- `UsageSnapshot`, `UsageMetric`, `AdministrativeStatus`, `ConnectivityPresence`, `CommercialPlan`, `ConsumptionLimit`, `StatusChange` (value objects)
- `Provider Adapter` (anti-corruption layer)
- `SubscriptionProvider` (Protocol/interface)
- `Provider Registry`
- `SIM Routing Map` (tabla)
- `KITE`, `TELE2`, `MOABITS` (enum Provider)
- `Profile`, `Company`, `CompanyProviderCredentials`
