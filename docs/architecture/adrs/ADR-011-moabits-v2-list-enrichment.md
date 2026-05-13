# ADR-011 — Moabits: enrichment v2 por defecto para `GET /v1/sims`

- **Estado**: Accepted
- **Fecha**: 2026-05-07
- **Decisores**: equipo backend
- **Relacionado**: ADR-002 (proxy en tiempo real), ADR-003 (adapter/ACL), ADR-004 (errores parciales), ADR-005 (resiliencia), ADR-006 (credenciales cifradas), ADR-010 (company codes explícitos)

## Contexto

El listado canónico `GET /v1/sims?provider=moabits` usa Orion v1
`GET /api/company/simList/{companyCode}` para descubrir las SIMs de cada
`company_code` persistido. Ese endpoint devuelve una fila liviana con
`iccid`, `simStatus`, `dataService` y `smsService`. Por eso las filas
Moabits del listado salen como `detail_level=summary` y muchos campos de
`normalized` quedan en `null`.

Orion Gateway API v2 expone endpoints bulk por lista de ICCIDs:

- `GET /api/v2/sim/{iccidList}` para identidad, plan, cliente y fechas.
- `GET /api/v2/sim/connectivity/{iccidList}` para conectividad en tiempo
  real.

La API v2 autentica con `X-API-KEY` directo. No documenta un endpoint de
listing por `companyCode`, por lo que no reemplaza a `simList` v1 como
fuente del universo de SIMs.

## Decisión

Mantener v1 como fuente de descubrimiento y estado administrativo del
listado, y usar v2 como enrichment degradable de la página ya paginada.
El flujo implementado es:

1. `list_subscriptions` consulta v1 `simList` por cada `company_code`
   hasta llenar la página solicitada.
2. Si `MOABITS_V2_ENRICHMENT_ENABLED=false`, devuelve exactamente la
   forma legacy v1-only. El default operativo es `true`: se intenta v2
   para enriquecer cada página obtenida desde v1.
3. Si el flag está activo, toma los ICCIDs de la página y llama en
   batches a:
   - `GET /api/v2/sim/{iccids}`
   - `GET /api/v2/sim/connectivity/{iccids}`
4. Los batches se trocean con `MOABITS_V2_MAX_BATCH` y se acotan con
   `MOABITS_V2_MAX_CONCURRENT_CHUNKS`.
5. Cada SIM se arma por merge:
   - v1 manda para `simStatus`, `dataService`, `smsService` y lista de
     servicios activos.
   - v2 detail aporta `msisdn`, `imsi`, `imei`, `product_*`,
     `clientName`, `companyCode`, límites y fechas.
   - v2 connectivity aporta `operator`, `country`, `rat_type`,
     `ip_address`, `mcc`, `mnc`, `data_session_id`,
     `session_started_at`, `usage_kb` y campos relacionados.

El enrichment es degradable: un fallo de v2 detail o connectivity se
registra en logs estructurados y produce mapas vacíos para ese batch; el
endpoint sigue devolviendo las SIMs de v1.

## Configuración

Valores actuales en `app/config.py`:

| Variable | Default | Uso |
|---|---:|---|
| `MOABITS_V2_ENRICHMENT_ENABLED` | `true` | Activa enrichment en listado Moabits |
| `MOABITS_V2_BASE_URL` | `https://apiv2.myorion.co` | Host v2 global |
| `MOABITS_V2_MAX_BATCH` | `125` | ICCIDs por request v2 |
| `MOABITS_V2_MAX_CONCURRENT_CHUNKS` | `2` | Concurrencia máxima de chunks |
| `MOABITS_V2_DETAIL_TIMEOUT_SECONDS` | `20.0` | Timeout de detail |
| `MOABITS_V2_CONNECTIVITY_TIMEOUT_SECONDS` | `15.0` | Timeout de connectivity |
| `MOABITS_V2_ENRICHMENT_CACHE_TTL_SECONDS` | `30.0` | Cache corto por ICCID para detail/connectivity |

El código actual reutiliza `x_api_key` de la credencial Moabits v1 para
v2. No existe todavía `x_api_key_v2` ni `base_url_v2` dentro de
`credentials_enc`; el host v2 es configuración de aplicación.

## Estado De Implementación

Implementado en:

- `app/config.py`: flags y timeouts `MOABITS_V2_*`.
- `app/providers/moabits/adapter.py`: `_v2_get`,
  `_v2_fetch_details_chunk`, `_v2_fetch_connectivity_chunk`,
  `_fetch_v2_enrichment` y merge en `list_subscriptions`.
- `tests/providers/test_moabits_adapter.py`: cobertura para full
  enrichment, flag apagado, detail 404, connectivity 5xx, ambos v2
  fallando, subset de ICCIDs ausentes, chunking y uso directo de
  `X-API-KEY`.

Pendientes conocidos:

- Confirmar con Moabits si `x_api_key` v1 y v2 siempre son la misma key.
  Si no lo son, agregar `x_api_key_v2` cifrado y preferirlo para v2.
- Confirmar rate limit y batch máximo real. Swagger declara
  `iccidList.maxLength=4000`, pero no documenta cuota ni cantidad máxima
  de ICCIDs.
- El adapter hoy mapea `smsLimit` legacy a `provider_fields.sms_limit`,
  pero no conserva todavía `smsLimitMo` / `smsLimitMt` v2 como campos
  separados. La documentación del contrato v2 sí los menciona.
- `_normalized_subscription` no expone todavía `usage_kb`,
  `data_session_id`, `mcc`, `mnc`, `charge_towards` ni
  `session_started_at` como campos canónicos; permanecen en
  `provider_fields`.
- Los fallos de v2 no se propagan a `SimListOut.partial` ni
  `failed_providers`; la degradación es por SIM mediante
  `provider_fields.enrichment_status` y logs.
- No hay `provider_call_audit` para `enrich_sim_v2_detail` ni
  `enrich_sim_v2_connectivity`; PR-15 sigue pendiente.

## Consecuencias

**Positivas**
- Se mantienen contratos públicos y rutas canónicas; v2 sólo puebla
  campos que antes eran `null`.
- El flag permite rollback operativo sin deploy.
- El listado no depende de v2 para funcionar.
- La autenticación v2 no comparte Bearer/JWT ni `_TOKEN_CACHE`.

**Negativas / mitigaciones**
- El listado puede tardar más cuando el flag está activo.
  - Mitigación: timeouts separados y degradación a v1.
- La concurrencia duplica llamadas por chunk (detail + connectivity).
  - Mitigación: semáforo global por request y batch configurable.
- La respuesta canónica `partial=false` puede ocultar degradaciones v2 a
  consumidores que no inspeccionen `provider_fields.enrichment_status`.
  - Mitigación: documentar el comportamiento y evaluar `failed_providers`
    en una PR posterior.

## Alternativas Consideradas

1. **Reemplazar v1 por v2**
   - Rechazada: v2 recibe ICCIDs, no `companyCode`; no puede descubrir el
     universo de SIMs.

2. **Enrichment background con persistencia local**
   - Rechazada por ADR-002: el sistema es proxy en tiempo real y no debe
     crear una tienda canónica de estado de SIM.

3. **Agregar endpoint específico de connectivity detail**
   - Rechazada: ya existe `GET /v1/sims/{iccid}/presence`; los datos v2
     enriquecen listado/detalle estándar y no justifican una ruta
     proveedor-específica.

## Cuándo Revisar

- Si Moabits publica un listing v2 por cliente/company que permita
  reemplazar `GET /api/company/simList/{companyCode}`.
- Si la latencia del listado enriquecido supera el SLO de UX.
- Si Moabits confirma que v2 requiere una API key distinta por tenant.
- Si producto necesita visibilidad explícita de degradaciones v2 en
  `SimListOut.partial` y `failed_providers`.
