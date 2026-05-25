# Phase 7 — Reporte de consistency check

> **Historical consistency check.** This was useful during the Phase 7 doc pass,
> but it is not the live source of truth. Use
> `docs/architecture/ARCHITECTURE.md`, `_context_state.json`, and the ADRs for
> current docs-vs-code alignment state.

## 1. Component names registry — verificación

Nombres canónicos registrados en `_context_state.json.component_names` y su uso en cada documento:

| Nombre canónico | arch-analysis | domain-model | context-map | patterns | ADRs | C4 | NFR |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| Subscriptions API | — | — | — | ✓ | ✓ | ✓ | ✓ |
| Subscription (aggregate) | — | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| status field | — | ✓ | — | — | ✓ | — | — |
| ConnectivityPresence | — | ✓ | — | — | — | — | — |
| UsageSnapshot | — | ✓ | — | — | ✓ | — | — |
| CommercialPlan | — | ✓ | — | — | — | — | — |
| ConsumptionLimit | — | ✓ | — | — | — | — | — |
| StatusChange | — | ✓ | — | — | ✓ | — | — |
| SubscriptionProvider (Protocol) | — | — | — | ✓ | ✓ | ✓ | — |
| Provider Adapter | — | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| Provider Registry | — | ✓ | — | ✓ | ✓ | ✓ | — |
| SIM Routing Map | — | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| UsageMetric | — | ✓ | — | — | ✓ | — | — |
| Profile | ✓ | ✓ | — | ✓ | ✓ | — | ✓ |
| Company | ✓ | ✓ | — | ✓ | ✓ | — | ✓ |
| CompanyProviderCredentials | — | ✓ | — | ✓ | ✓ | ✓ | ✓ |
| KITE / TELE2 / MOABITS | — | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| ControlOperation (Purge) | — | ✓ | — | — | ✓ | — | — |

**Resultado**: sin divergencias — todos los documentos usan los mismos términos. Vocabulario de proveedor (`icc`, `lifeCycleStatus`, `simStatus`, `iccidList`, `Edit Device Details`, etc.) aparece **sólo** en secciones que explícitamente hablan del proveedor y dentro del dominio de los adapters, nunca en el modelo canónico. ✓

## 2. Referencias cruzadas entre documentos

| Fuente | Referencia | Destino existe |
|---|---|---|
| domain-model.md §3 | `SIM Routing Map` (tabla) | ✓ definida en §3 y referenciada en ADR-002 |
| domain-model.md §6.1 | `UnsupportedOperation` | ✓ ADR-004 jerarquía |
| patterns-decisions.md D3 | `SimRoutingMap` schema | ✓ domain-model §3 |
| patterns-decisions.md D5 | `require_roles` | ✓ ADR-008 matriz |
| patterns-decisions.md D8 | `company_provider_credentials` | ✓ ADR-006 DDL |
| patterns-decisions.md D9 | RFC 7807 | ✓ ADR-004 |
| ADR-003 | `FakeProvider` | ✓ ADR-009 Capa 3 |
| ADR-005 | `ProviderUnavailable` etc. | ✓ ADR-004 |
| ADR-007 | `Idempotency-Key` | ✓ ADR-007 §6 + ADR-008 §6 coherentes |
| ADR-008 | `audit_log` | ✓ ADR-008 §5 DDL |
| nfr-analysis.md | `company_provider_credentials`, `audit_log`, `idempotency_keys`, `sim_routing_map` | ✓ todos referenciados en ADR-002/006/007/008 |

**Resultado**: sin referencias colgantes.

## 3. Decisiones que viajan a ARCHITECTURE.md

Toda decisión registrada en `_context_state.json.decisions_made` tiene su ADR generado:

| Decisión | ADR | Status |
|---|---|---|
| Modular Monolith | ADR-001 | ✓ |
| Real-time proxy | ADR-002 | ✓ |
| ACL + Adapter | ADR-003 | ✓ |
| Error model | ADR-004 | ✓ |
| Resilience + cache | ADR-005 | ✓ (revisado en Phase 7) |
| Encrypted credentials | ADR-006 | ✓ |
| API versioning | ADR-007 | ✓ |
| Auth + RBAC + audit | ADR-008 | ✓ |
| Testing strategy | ADR-009 | ✓ |

## 4. Elementos de diagramas vs modelo de dominio

C4 Container + Component names vs glosario:

- `Subscription Aggregation Module` ↔ bounded context "Subscription Aggregation" ✓
- `Provider Adapters` ↔ bounded context "Provider Integration" ✓
- `Identity & Tenancy Module` ↔ bounded contexts "Identity & Access" + "Tenancy" (fusionados por practicidad en el diagrama — correcto al nivel de container)
- `SubscriptionFetcher`, `SubscriptionSearchService`, `SubscriptionOperationService` ↔ domain services del §4 de domain-model.md ✓
- `sim_routing_map` ↔ `SimRoutingMap` entity ✓

**Ajuste**: el domain-model ha unificado la operación de control en una única `ControlOperation` canónica (`purge`) y el L3 usa `SubscriptionOperationService` para agrupar esta responsabilidad. La divergencia anterior se resolvió documentando el mapeo proveedor→operación en `domain-model.md §6.1` y registrando la ruta implementada `POST /v1/sims/{iccid}/purge`.
| NFR | Estrategia | Decisión origen |
|---|---|---|
| NFR-P1/P2 | timeout + cache single-flight + circuit breaker | ADR-005 |
| NFR-A2 | circuit breaker + bulkhead | ADR-005 |
| NFR-Sec1 | tabla cifrada + scrubber | ADR-006 |
| NFR-Sec2/3 | fix AP-8 / AP-1 | Phase 1 deuda técnica |
| NFR-Sec4 | audit_log | ADR-008 |
| NFR-Sec5 | filtro company_id en routing | ADR-002 + ADR-008 |
| NFR-Sec7 | rate limit token bucket | ADR-005 D7 |
| NFR-O1..5 | structlog + Prometheus + OpenTelemetry | ADR-005 D10 |
| NFR-M1..3 | import-linter + pytest gates | ADR-001 + ADR-009 |
| NFR-D1/D2 | credenciales activas + idempotency table | ADR-006 + ADR-007 |
| NFR-C1/C2 | single-flight + modular monolith | ADR-005 + ADR-001 |

**Resultado**: cada NFR tiene trazabilidad a al menos un ADR. ✓

## 6. Open questions críticas

Ninguna de estas bloquea el diseño; bloquean **implementación inicial**:

| # | Pregunta | Impacto si no se resuelve |
|---|---|---|
| OQ-1 | ¿Carga inicial del SIM Routing Map: CSV o lazy discovery? | Bajo — lazy funciona, CSV es operacionalmente más sano |
| OQ-2 | ¿Cómo se mapea `Company` local ↔ `endCustomerId`/`accountId`/`companyCodes` de cada proveedor? | Medio — queda capturado en `company_provider_credentials.account_scope` pero hay que poblarlo. Esencial para routing |
| OQ-3 | ¿Sandbox de los proveedores? | Bajo — afecta sólo Capa 5 de testing (E2E) |
| OQ-4 | ¿Stack de observabilidad (Prom/Grafana vs Datadog)? | Bajo — decisión de infra, no de arquitectura |
| OQ-5 | ¿Retención legal del `audit_log`? | Bajo — default 1 año, ajustable |
| OQ-6 | ¿Frontend ya envía `Idempotency-Key`? | Bajo — si no, backend devuelve 400 con `code: idempotency_key_required` |
| OQ-7 | ¿SLA contractual de cada proveedor? | Medio — calibra timeouts y umbrales de circuit breaker |

## 7. Correcciones aplicadas en Phase 7

1. **ADR-005** — caché de `get_usage` explícitamente deshabilitada con nota `[REVISED in Phase 7: NFR-C1 + semántica de consumo]`.
2. **domain-model.md §4** — `SubscriptionOperationService` cubre `purge` y la ruta implementada es `POST /v1/sims/{iccid}/purge`.

## 8. Checklist final

- [x] Todos los nombres de `component_names` se usan uniformemente.
- [x] Ningún vocabulario de proveedor aparece en el dominio.
- [x] Toda decisión significativa tiene ADR.
- [x] Todo elemento de diagrama existe en el modelo de dominio o está justificado.
- [x] Todo NFR está anclado a al menos una decisión.
- [x] Open questions están capturadas y triageadas.
- [x] Las revisiones cross-phase se etiquetaron con `[REVISED in Phase N: …]`.
- [x] Todo elemento de diagrama existe en el modelo de dominio o está justificado.
