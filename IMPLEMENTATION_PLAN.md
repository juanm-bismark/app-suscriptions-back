# Plan de implementación — alineación con docs de proveedor y arquitectura canónica

**Fuente:** `INTEGRATION_REVIEW.md`, NotebookLM Kite/Tele2, `moabits.md` y estado actual del código.
**Equipo:** 1 ingeniero.
**Fecha:** 2026-05-05.
**Estado:** roadmap vivo. Mantiene la fachada canónica `/v1/sims/**`; no introduce endpoints por proveedor salvo capacidades opcionales justificadas.

---

## 0. Principios no negociables

1. El frontend llama endpoints canónicos, no endpoints Kite/Tele2/Moabits.
2. El vocabulario de proveedor vive sólo en adapters, mappers, DTOs y documentación de integración.
3. Si dos proveedores tienen distinta API para la misma intención operativa, se conserva una operación canónica y el adapter traduce.
4. Si una capacidad existe sólo en un proveedor, se expone como capacidad opcional, nunca como rama de lógica en el frontend.
5. Consumo, límites y detalles administrativos no se convierten en endpoints independientes si la fuente documental los define como campos anidados.

---

## 1. Ya corregido — Kite SOAP contract hygiene

**Estado:** implementado y cubierto por tests.

- [app/providers/kite/client.py](app/providers/kite/client.py) — `username/password` son opcionales. El envelope SOAP sólo incluye `WS-Security UsernameToken` cuando ambos están configurados.
- [app/providers/kite/client.py](app/providers/kite/client.py) — `getSubscriptions` emite el body en orden WSDL: `maxBatchSize`, `startIndex`, `searchParameters`.
- [app/providers/kite/dto.py](app/providers/kite/dto.py) — documenta cert-only y WSSE opcional.
- [app/tenancy/models/credentials.py](app/tenancy/models/credentials.py) — documenta que el PFX va en `credentials_enc`, no en `.env`.
- Tests: `pytest tests/providers/test_kite_writes.py tests/providers/test_kite_adapter.py tests/providers/test_kite_faults.py`.

**Criterio documental:** NotebookLM/Kite muestra identidad por certificado SSL público/mTLS. No hay evidencia de `UsernameToken` en los ejemplos del binding; por eso WSSE queda como compatibilidad opcional por deployment, no requisito base.

---

## 2. PR-7 · Normalizar unidades de uso sin romper la fachada

**Justificación:** el dominio conservaba nombres ambiguos (`data_used_mb`, `voice_minutes`) mientras Kite/Tele2 trabajan principalmente en bytes y la unidad de voz de Tele2 sigue pendiente de confirmación.

**Cambios:**
- [app/subscriptions/domain.py](app/subscriptions/domain.py) — agregar campos explícitos o migrar a `data_used_bytes` y `voice_seconds`.
- [app/subscriptions/schemas/sim.py](app/subscriptions/schemas/sim.py) — mantener `usage_metrics[]` como contrato principal; dejar totales legacy sólo si se requiere compatibilidad.
- [app/providers/kite/mappers.py](app/providers/kite/mappers.py) — conservar `ConsumptionType.value` con unidad documentada.
- [app/providers/tele2/adapter.py](app/providers/tele2/adapter.py) — confirmar unidad de voz antes de convertir; no etiquetar como `minutes` sin evidencia.
- [app/providers/moabits/adapter.py](app/providers/moabits/adapter.py) — convertir datos nativos MB a unidad canónica y preservar `data_mb` en `provider_metrics`.

**Arquitectura:** sigue usando `GET /v1/sims/{iccid}/usage`; el adapter decide si la fuente es `getSubscriptions`, `Get Device Usage` o `simUsage`.

---

## 3. PR-8 · Moabits status map tolerante y verificado

**Justificación:** `moabits.md` usa valores tipo Cisco (`ACTIVATED`, `TEST_READY`, `PURGED`, etc.), pero el adapter histórico observó valores tipo `Active`, `Ready`, `Suspended`. Orion API 2.0.0 confirma que las transiciones escribibles son sólo active/suspend/purge; hasta tener payload real de status, el mapper debe aceptar ambos casing.

**Cambios:**
- [app/providers/moabits/status_map.py](app/providers/moabits/status_map.py) — normalizar con `native.strip().upper()`.
- Cubrir: `ACTIVATED`, `ACTIVE`, `READY`, `TEST_READY`, `SUSPENDED`, `PURGED`, `INVENTORY`, `DEACTIVATED`, `REPLACED`, `RETIRED`.
- Mantener `native_status` crudo para UI/soporte.
- Agregar fixture real cuando exista payload de producción/sandbox.

**Arquitectura:** el enum canónico absorbe variaciones de proveedor; el frontend no debe comparar `simStatus` nativo.

---

## 4. PR-9 · Capabilities endpoint para UI sin lógica por proveedor

**Justificación:** el frontend necesita saber qué mostrar o habilitar sin codificar reglas `if provider == ...`.

**Cambios:**
- Nueva ruta canónica: `GET /v1/providers/{provider}/capabilities`.
- Respuesta por proveedor con `supported`, `not_supported`, `requires_feature_flag`, `requires_confirmation` y razón.
- Capacidades mínimas: `list_subscriptions`, `get_subscription`, `get_usage`, `get_presence`, `set_administrative_status`, `purge`, `status_history`, `aggregated_usage`, `plan_catalog`, `quota_management`.
- Incluir semántica de `purge`: Kite → `networkReset`, Tele2 → `status=PURGED`, Moabits → `PUT /api/sim/purge/` con `iccidList`.

**Arquitectura:** no crea endpoints específicos; sólo publica metadata para renderizar la experiencia correcta.

---

## 5. PR-10 · Filtros canónicos de listado y mapeo a search params

**Justificación:** Kite y Tele2 documentan filtros ricos, pero la API pública hoy sólo recibe `provider`, `cursor`, `limit`.

**Cambios:**
- Extender `GET /v1/sims` con filtros canónicos seguros: `status`, `modified_since`, `modified_till`, `iccid`, `imsi`, `msisdn`, `custom`.
- Kite adapter: mapear a `searchParameters` whitelisted.
- Tele2 adapter: mapear a `modifiedSince`, `modifiedTill`, `status`, custom fields y paginación.
- Moabits adapter: aplicar sólo lo verificable; si un filtro no existe en Moabits, devolver `not_supported` o ignorarlo explícitamente según contrato.

**Arquitectura:** una sola ruta de búsqueda; mapeo de proveedor dentro del adapter.

---

## 6. PR-11 · Parámetros de uso por ventana sin endpoint por proveedor

**Justificación:** Tele2 `Get Device Usage` acepta `startDate`, `endDate`, `metrics`; Moabits usa fechas propias; Kite sólo expone consumo corriente embebido.

**Cambios:**
- `GET /v1/sims/{iccid}/usage` acepta `start_date`, `end_date`, `metrics`.
- Tele2: convertir a `YYYYMMDD`, validar rango máximo 30 días.
- Moabits: convertir a `initialDate` / `finalDate` si el formato queda confirmado.
- Kite: si se pide ventana histórica, responder `UnsupportedOperation` claro o devolver consumo corriente con metadata `period_scope=current_cycle`.

**Arquitectura:** sigue siendo un endpoint canónico; diferencias viajan en `provider_metrics` y metadata.

---

## 7. PR-12 · Tele2 sessionDetails sin fallback extra

**Justificación:** `sessionDetails` es el endpoint documentado para presencia. Si falla con 404 o sin sesión, no hace falta una segunda llamada a Device Details salvo que una decisión de producto lo pida.

**Cambios:**
- [app/providers/tele2/adapter.py](app/providers/tele2/adapter.py) — eliminar fallback a `/devices/{iccid}` dentro de presencia.
- `404` de `sessionDetails` → `ConnectivityState.UNKNOWN`.
- Preservar `dateSessionStarted` y `lastSessionEndTime` en `provider_metrics` o schema extendido.

**Arquitectura:** `GET /v1/sims/{iccid}/presence` sigue siendo único para los tres proveedores.

---

## 8. PR-13 · Límites y detalles administrativos como payload, no endpoints prematuros

**Justificación:** Kite y Tele2 documentan límites dentro de detalle/edición; `moabits.md` también dice que límites viajan dentro del detalle del dispositivo/cuenta.

**Cambios:**
- Agregar `detail_level` y `normalized` a `SubscriptionOut`, manteniendo compatibilidad con los campos top-level y `provider_fields`.
- `normalized` contiene bloques homogéneos: `identity`, `status`, `plan`, `customer`, `network`, `hardware`, `services`, `limits`, `dates`, `custom_fields`.
- Kite: preservar `limit`, `value`, `thrReached`, `enabled`, `trafficCut` de `consumptionDaily/monthly`.
- Tele2: preservar `overageLimitOverride`, `testReadyDataLimit`, `testReadySmsLimit`, `testReadyVoiceLimit`, `testReadyCsdLimit`.
- Moabits: preservar `dataLimit`, `smsLimit`, `product_*`, `planStartDate`, `planExpirationDate`.
- Tele2 listing: después de `Search Devices`, llamar `Get Device Details` sólo para las primeras 5 SIMs de la página y devolverlas con `detail_level=detail`; el resto queda `summary`.
- Documentar en OpenAPI y docs que `summary` no implica ausencia real de campos como `msisdn`/`imsi`, sólo que el endpoint de listado no los trajo.

**No hacer:** `GET /v1/sims/{iccid}/limits` salvo que producto requiera una pantalla independiente y el contrato lo declare como vista derivada, no endpoint proveedor.

---

## 9. PR-14 · Rotación y alerta de certificados Kite

**Justificación:** cada cliente/company debe usar su propio certificado Kite vigente. Los PFX no deben vivir en `.env`.

**Cambios:**
- Flujo operativo para actualizar `company_provider_credentials.credentials_enc` con `client_cert_pfx_b64` y `client_cert_password`.
- Endpoints `GET/POST/PATCH /v1/companies/me/credentials/**` disponibles para `manager` y `admin`; `DELETE` sólo para `admin`.
- Moabits: `GET /companies/me/credentials/moabits/companies/discover` disponible para `manager`/`admin`; `PUT /companies/me/credentials/moabits/company-codes` sólo para `admin` porque cambia el scope operativo de la fuente. Los `company_codes` se guardan en `provider_source_configs`, no en `credentials_enc`.
- Usar siempre `companies/me` para que un `manager` no pueda operar otro tenant.
- Actualizar `rotated_at`.
- Guardar en `account_scope` sólo metadata no secreta: `environment`, `end_customer_id`, `cert_expires_at`.
- Job/alerta por expiración a 30/15/7 días.
- Tests de decrypt/load PFX con fixture sintético.

**Arquitectura:** credenciales por tenant; no certificado global compartido salvo decisión comercial explícita.

---

## 10. PR-15 · Audit trail de llamadas a proveedores

**Justificación:** hoy se conservan algunos IDs en errores, pero falta traza persistente de llamadas exitosas/fallidas.

**Cambios:**
- Nueva tabla `provider_call_audit` o extensión de auditoría existente.
- Registrar: `provider`, `operation`, `company_id`, `iccid?`, `latency_ms`, `http_status?`, `provider_request_id?`, `provider_error_code?`, `outcome`, `created_at`.
- Kite: guardar `SOATransactionID` / `SOAConsumerTransactionID` cuando esté disponible.
- Tele2: registrar `errorCode` / `errorMessage`; confirmar si hay request-id.
- Moabits: confirmar si devuelve request-id/correlator.

---

## 11. Capacidades opcionales de v2

Estas operaciones existen en documentos fuente o resúmenes, pero no forman parte del core canónico actual.

| Capacidad | Fuente | Estrategia |
|---|---|---|
| Tele2 `Get Aggregated Usage Details` | Resource Catalog | Nuevo capability protocol `AggregatedUsageProvider`; no mezclar con per-SIM `usage`. |
| Tele2 `Get Service Type Details` / TMF | Resource Catalog | `PlanCatalogProvider` cuando exista UI de planes. |
| Kite Reports API | WSDL | `ReportProvider` async para reconciliación, no para lecturas por SIM. |
| Moabits aggregated usage | Orion API 2.0.0 `GET /api/usage/companyUsage` | No exponer en v1; futura capacidad `AggregatedUsageProvider` si producto lo pide. |
| Moabits SIM limits / name update | Orion API 2.0.0 `PUT /api/sim/setLimits/`, `PUT /api/sim/details/{iccid}/name/` | No exponer en v1; futura capacidad de administración avanzada de SIM. |
| Kite `getStatusHistory` | WSDL | Exponer como capability opcional; Tele2/Moabits devuelven `not_supported`. |

---

## 12. PR-16 · Moabits v2 enrichment para listado

**Estado:** implementado detrás de feature flag. Documentado en [ADR-011](docs/architecture/adrs/ADR-011-moabits-v2-list-enrichment.md).

**Decisión:** `GET /v1/sims?provider=moabits` conserva v1 `GET /api/company/simList/{companyCode}` como fuente del universo de SIMs y usa Orion Gateway v2 sólo para enriquecer los ICCIDs de la página ya paginada.

**Implementado:**
- [app/config.py](app/config.py) — `MOABITS_V2_ENRICHMENT_ENABLED`, `MOABITS_V2_BASE_URL`, `MOABITS_V2_MAX_BATCH`, `MOABITS_V2_MAX_CONCURRENT_CHUNKS`, `MOABITS_V2_DETAIL_TIMEOUT_SECONDS`, `MOABITS_V2_CONNECTIVITY_TIMEOUT_SECONDS`.
- [app/providers/moabits/adapter.py](app/providers/moabits/adapter.py) — cliente v2 con `X-API-KEY` directo, detail/connectivity bulk por chunks, semáforo de concurrencia y merge por ICCID.
- [tests/providers/test_moabits_adapter.py](tests/providers/test_moabits_adapter.py) — cobertura de flag apagado, enrichment completo, fallos parciales, 404 detail, 5xx connectivity, chunking y header `X-API-KEY` sin Bearer.

**Pendientes / incongruencias detectadas contra la propuesta técnica:**
- La propuesta sugiere `base_url_v2` y posible `x_api_key_v2` dentro de credenciales cifradas; el código actual usa `MOABITS_V2_BASE_URL` global y reutiliza `x_api_key`.
- La propuesta pide propagar fallos de v2 en `partial=true` / `failed_providers`; el código actual degrada por SIM con `provider_fields.enrichment_status` y logs, pero `SimListOut.partial` sigue en `false` para el listado provider-scoped.
- La propuesta menciona `smsLimitMo` y `smsLimitMt`; el mapper actual preserva sólo `smsLimit` como `sms_limit`, por lo que los límites MO/MT de v2 aún no quedan expuestos.
- La propuesta lista `usage.last_data_kb` y varios campos de red como canónicos; el código actual los conserva en `provider_fields` (`usage_kb`, `mcc`, `mnc`, `data_session_id`, etc.) y `_normalized_subscription` no los eleva.
- La propuesta habla de registrar `enrich_sim_v2_detail` y `enrich_sim_v2_connectivity` en `provider_call_audit`; PR-15 sigue pendiente y no existe esa tabla.

---

## 13. Validación con proveedores

| Tarea | Proveedor | Bloquea |
|---|---|---|
| Confirmar host productivo y certificado vigente | Kite/Telefónica | smoke test real |
| Confirmar si algún deployment exige WSSE además de mTLS | Kite/Telefónica | sólo despliegues legacy |
| Confirmar semántica de `forceRetired` | Kite/Telefónica | cualquier flujo de retiro |
| Confirmar CPS / rate limit | Kite/Telefónica | tuning del circuit breaker |
| Confirmar host productivo | Tele2 | smoke test real |
| Confirmar unidad de voz | Tele2 | PR-7 |
| Obtener diagrama formal de transiciones | Tele2 | writes en producción |
| Obtener payload real `serviceStatus` | Moabits | PR-8 |
| Confirmar límites de paginación/rate limit en company list | Moabits | listing en producción |
| Confirmar payload/unidades de `simUsage` y `companyUsage` | Moabits | métricas y futura analítica |
| Confirmar si v2 reutiliza la misma `X-API-KEY` que v1 | Moabits | PR-16 en producción |
| Confirmar batch máximo y rate limit v2 | Moabits | PR-16 en producción |

---

## 14. Secuencia recomendada

```text
Semana 1   PR-8  Moabits status casing
           PR-12 Tele2 session cleanup
           PR-9  Provider capabilities

Semana 2   PR-10 filtros canónicos de listado
           PR-11 usage con ventana/metrics
           PR-14 cert expiry metadata + alerta

Semana 3   PR-13 límites/planes como payload tipado
           PR-15 audit trail de provider calls

Bloqueado  PR-7  rename de unidades públicas
           (espera confirmación unidad voz Tele2 o requiere versión de API)

Hecho      PR-16 Moabits v2 enrichment detrás de feature flag
```

---

## 15. Criterios de aceptación

- [ ] No se introduce lógica `if provider` en frontend.
- [ ] Cada endpoint público mantiene vocabulario canónico.
- [ ] Cada adapter demuestra con tests la llamada nativa documentada.
- [ ] `pytest` pasa completo.
- [ ] `ruff check` pasa en archivos tocados.
- [ ] `git diff --check` pasa.
- [ ] Writes siguen detrás de `LIFECYCLE_WRITES_ENABLED=false` por defecto hasta smoke test.
- [ ] Capacidades no confirmadas devuelven `not_supported`, no endpoints inventados.

---

## 16. Riesgos pendientes

| Riesgo | Probabilidad | Impacto | Mitigación |
|---|---|---|---|
| Unidad de voz Tele2 no confirmada | Media | Métricas mal normalizadas | Bloquear rename público o preservar campo nativo. |
| Moabits emite status con casing distinto | Media | Estado `unknown` | Mapper tolerante + payload real. |
| Moabits company listing no documenta paginación nativa | Media | Memoria/latencia en cuentas grandes | Mantener paginación local acotada; pedir límite/paginación oficial al proveedor. |
| Moabits v2 puede requerir key distinta a v1 | Media | Enrichment 401/403 en producción | Confirmar con proveedor; si aplica, agregar `x_api_key_v2` cifrada. |
| Moabits v2 failures no aparecen en `failed_providers` | Media | UI no ve degradación parcial | Usar `provider_fields.enrichment_status` hoy; evaluar propagación a `SimListOut.partial`. |
| Certificado Kite expira por tenant | Alta | Caída total de ese tenant | `cert_expires_at` + alertas 30/15/7 días. |
| Endpoints opcionales se mezclan con core | Media | UI compleja y contratos ambiguos | Capability protocols y `not_supported`. |
