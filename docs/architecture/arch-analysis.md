# Phase 1 — Análisis del Código Existente

> **Tipo de proyecto**: brownfield
> **Alcance**: el repositorio `back/` contiene la base FastAPI sobre la que se va a construir la integración con los proveedores (Kite, Tele2, Moabits). Ese módulo de proveedores aún no existe — esta fase analiza la base sobre la que va a apoyarse y los riesgos que arrastra.

---

## 1. Inventario de módulos (observado)

| Módulo | Responsabilidad observada | Acoplamiento externo |
|---|---|---|
| `app/main.py` | Bootstrap FastAPI, registro de routers, lifespan de engine, exception handler global | `routers/*`, `database` |
| `app/config.py` | Settings via `pydantic-settings`, cacheado con `lru_cache` | env vars |
| `app/database.py` | Engine SQLAlchemy async global, factoría de sesiones, `get_db` dependency | SQLAlchemy, asyncpg |
| `app/dependencies.py` | Validación JWT, carga de `Profile`, `require_roles` factory | DB, JWT |
| `app/auth_utils.py` | Hash bcrypt, generación de tokens (acceso + refresh) | bcrypt, PyJWT |
| `app/models/*` | `User`, `Profile`, `Company`, `CompanySettings`, `RefreshToken`. SQLAlchemy ORM | DB |
| `app/schemas/*` | DTOs Pydantic (entrada/salida HTTP) | Pydantic |
| `app/routers/auth.py` | signup / login / refresh / logout. Maneja también creación de `Company` en signup público | DB, auth_utils |
| `app/routers/users.py`, `me.py`, `companies.py` | CRUD/consulta sobre el modelo interno | DB |

**Observación**: la base es coherente, pequeña, y suficiente como punto de partida. El stack `httpx` ya está declarado en `requirements.txt` aunque todavía no se usa: el equipo ya anticipó el rol de cliente HTTP saliente.

---

## 2. Mapa de dependencias

```
main → routers/* → dependencies → models / database
                      ↘ config
auth_router → auth_utils + models (User, Profile, Company, CompanySettings, RefreshToken)
```

- **Hotspot 1**: `app/routers/auth.py:74-150` — el endpoint `signup` mezcla **tres responsabilidades** (alta de usuario, alta de compañía, gestión de roles según contexto). Esto será un problema cuando se agregue la noción de "credenciales del proveedor por compañía", porque la tentación será meterlo aquí también.
- **Hotspot 2**: `app/database.py` mantiene `_engine` como variable de módulo (singleton implícito). Funciona, pero acopla testabilidad y dificulta inyectar engines de prueba sin monkeypatch.
- **Hotspot 3**: el manejo de errores de `IntegrityError` en signup (`auth.py:140-148`) hace pattern-matching sobre el mensaje de la excepción. Frágil ante cambios de driver/Postgres.

---

## 3. Bounded contexts implícitos (observado)

Aunque la estructura de carpetas es plana (`models/`, `routers/`, `schemas/`), se distinguen **dos contextos** ya emergentes:

1. **Identity & Access** — `User`, `Profile`, `RefreshToken`, `auth_utils`, `dependencies`, `routers/auth.py`, `routers/me.py`, `routers/users.py`
2. **Tenancy** — `Company`, `CompanySettings`, `routers/companies.py`

El contexto que **falta crear** y es el objeto de esta arquitectura:

3. **Subscription Aggregation** — sin código, sin modelos. Es el dominio nuevo.

> **Inferencia**: la organización por `models/`, `routers/`, `schemas/` es *layered*, no *modular*. Para 3 contextos chicos es aceptable; para crecer va a ser apretado. Ver §6.

---

## 4. Anti-patrones detectados (con evidencia, no especulación)

| # | Anti-patrón | Evidencia | Severidad |
|---|---|---|---|
| AP-1 | **CORS abierto a `*` con `allow_credentials=True`** | `app/main.py:26-32` | crítica (la combinación es rechazada por navegadores; además abre a CSRF cuando se permita un origin real) |
| AP-2 | **Engine global mutable** (`_engine`, `_session_factory` como variables de módulo) | `app/database.py:5-12` | media (testabilidad y posibilidad de race en hot-reload) |
| AP-3 | **Pattern matching sobre mensaje de `IntegrityError`** | `app/routers/auth.py:142-148` | media (frágil; falla silenciosamente ante un constraint nuevo) |
| AP-4 | **Sin versión en URL** — los routers cuelgan de `/auth`, `/users`, etc. | `app/main.py:44-47` | alta para esta arquitectura: sin `/v1/` desde el día 0, romper el contrato será costoso cuando el modelo canónico evolucione |
| AP-5 | **Excepciones `HTTPException` lanzadas con string libre**, sin taxonomía | varios | media (responses heterogéneas; difícil de mapear a errores de proveedor cuando llegue ACL) |
| AP-6 | **Logging mínimo**, sin `request_id`/`trace_id` ni JSON estructurado | `app/main.py:13, 36-41` | alta (un sistema que orquesta 3 APIs externas necesita correlación obligatoria desde el día 1) |
| AP-7 | **Tabla `company_settings` con JSONB sin esquema** | `app/models/company_settings.py:19` | media (será tentador meter ahí las credenciales por proveedor; ver §7) |
| AP-8 | **Refresh token en texto plano en DB** (`token` columna `String`) | `app/models/refresh_token.py` | alta (si se filtra el snapshot de DB se filtran sesiones activas; debería estar hasheado) |

---

## 5. Registro de deuda técnica

| Severidad | Ítem | Pago recomendado |
|---|---|---|
| crítica | AP-1 CORS `*` + `credentials` | Reemplazar por lista explícita derivada de `settings.cors_origins` antes de exponer a Internet |
| alta | AP-4 sin versionado en URL | Mover routers a `/v1/...` antes de publicar nada de proveedores |
| alta | AP-6 logging sin correlación | Adoptar `structlog` o `loguru` con middleware que inyecte `request_id` en contexto |
| alta | AP-8 refresh token en plano | Hashear (sha256/bcrypt) antes de persistir; la tabla guarda hash, el cliente recibe el original |
| media | AP-2 engine global | Encapsular en un container/`Lifespan`-state accesible vía `request.state` |
| media | AP-3 string-matching de errores | Capturar excepción asyncpg específica (`UniqueViolationError`) usando `e.orig` |
| media | AP-5 errores ad-hoc | Crear jerarquía de excepciones de dominio + handler global que serialice a HTTP |
| media | AP-7 JSONB sin esquema en `company_settings` | No usarlo para credenciales de proveedor — crear tabla dedicada `company_provider_credentials` (ver Phase 2) |

---

## 6. Riesgos y oportunidades

### Riesgos
- **R1**: agregar el dominio "Subscription" sobre la estructura layered actual (`models/`, `routers/`, `schemas/`) hará que un mismo cambio toque archivos en tres carpetas distintas. Crece la fricción mental con cada proveedor nuevo.
- **R2**: sin idempotencia ni circuit breaker, una caída de Tele2 puede saturar el pool de conexiones de FastAPI y degradar también las consultas a Kite y Moabits (failure propagation).
- **R3**: el modelo de error actual es `HTTPException(500, "Invalid credentials")`. Cuando un proveedor responda 503, hay que decidir: ¿503 al cliente? ¿502? ¿qué pasa si uno responde 200 y otro 503 en una agregación?
- **R4**: el repo no tiene tests automatizados visibles. Para una capa anti-corrupción, los **tests de mappers son el activo principal de la arquitectura**: cada formato de proveedor cambia, y los tests son la red. Esta deuda hay que pagarla con la primera línea de código de proveedor.
- **R5**: 134 612 SIMs × 3 proveedores = riesgo de "fan-out por defecto". Un endpoint mal diseñado que diga "dame la SIM XYZ" sin saber su proveedor terminará llamando a los 3. Hay que **resolver la afinidad SIM→proveedor antes** de cada operación (ver Phase 2 §SIM Routing Map).

### Oportunidades
- **O1**: stack ya 100% async (FastAPI + asyncpg + httpx) — perfecto para I/O de proveedores externos sin esfuerzo extra.
- **O2**: `Company` ya existe como tenant. Anclar credenciales por proveedor a `Company` es un cambio aditivo, no requiere refactor del modelo de identidad.
- **O3**: RBAC (`AppRole`) ya implementado — sólo hay que mapear cuáles roles pueden ejecutar acciones mutantes canónicas (`purge`) y sus primitivas proveedor (`networkReset`, `Edit Device Details {PURGED}`, rutas dedicadas).
- **O4**: la base es chica (≈400 líneas) — la reorganización a paquetes por contexto cuesta poco hoy y mucho dentro de seis meses.

---

## 7. Hechos vs inferencias

**Hechos observados** (leídos directamente del código):
- Todo lo etiquetado AP-1..AP-8 con cita de archivo/línea.
- La existencia de `Company`, `CompanySettings`, `Profile.company_id`, `AppRole`.
- `httpx` declarado en `requirements.txt` sin uso aún.

**Inferencias** (no derivadas linealmente del código):
- `[INFERENCIA]` La arquitectura layered está bien para el tamaño actual pero no escalará a múltiples proveedores: se basa en que la base es pequeña y los módulos pocos.
- `[INFERENCIA]` `CompanySettings.settings: JSONB` será tentado para guardar credenciales de proveedor — desaconsejado por: ausencia de esquema, dificultad de auditoría, y violación de least-privilege (cualquier query a settings expone secretos).
- `[ASSUMPTION: team_size < 5]` Equipo chico → modular monolith preferido a microservicios per-proveedor.
