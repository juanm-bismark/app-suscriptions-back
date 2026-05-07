# ADR-008 — Reutilizar JWT existente + RBAC por `AppRole` + scope por `Company` + auditoría de mutaciones

- **Estado**: Accepted
- **Fecha**: 2026-04-22
- **Decisores**: equipo backend
- **Relacionado**: ADR-006 (credenciales), ADR-007 (API)

## Contexto

El repo ya tiene autenticación funcional: JWT propio (PyJWT, HS256), bcrypt para passwords, refresh tokens en DB con rotación, y RBAC via enum `AppRole = {public, admin, manager, member}`. Los `Profile` tienen `company_id` (multi-tenant).

Hay que decidir: **reusar lo existente** vs **migrar a un IdP gestionado** (Supabase Auth, Auth0, Cognito) antes de exponer el dominio de subscriptions.

Hay que definir: qué roles pueden ejecutar las **operaciones mutantes** de subscriptions (`purge` — alias proveedor: `network_reset`, `Edit Device Details {status: PURGED}` o ruta dedicada de purga), porque éstas tienen efecto en la red de telco real (cuesta dinero, afecta servicio del cliente final). También hay que separar esas operaciones destructivas de la gestión de credenciales propias del tenant, que puede delegarse a `manager` sin darle control sobre otros tenants ni sobre writes de SIM.

## Decisión

### 1. Reutilizar el JWT propio

- Mantener `app/dependencies.get_current_profile` y `require_roles(*roles)`.
- Mantener `Profile.company_id` como **el** scope de tenant — toda operación de subscriptions filtra implícitamente por este `company_id`.
- HS256 con `JWT_SECRET` en env, `JWT_EXPIRE_MINUTES` configurable (actual 60 min).

### 2. Mitigaciones de seguridad — estado actual

| ID | Item | Estado | Detalles |
|---|---|---|---|
| AP-1 | CORS explícito (no `*`) | ✅ **Implementado** | `CORS_ORIGINS` en `.env`, parseado en `app/config.py` |
| AP-8 | Refresh tokens hasheados | ✅ **Implementado** | sha256 en `app/identity/auth_utils.hash_refresh_token()` antes de persistir |
| sec-1 | HS256 con plan de rotación | ⏳ Documentado | Futuro RS256 con `kid`; procedimiento en lugar |
| sec-2 | Auditoría de mutaciones | ✅ Implementado | `audit_log` queda como bitácora genérica; `lifecycle_change_audit` registra writes de status/purge con actor, request_id, outcome y latencia |

### 3. Matriz de autorización para Subscriptions y credenciales

| Operación | Endpoint | `member` | `manager` | `admin` |
|---|---|---|---|---|
| Listar mis SIMs | `GET /v1/sims` | ✓ | ✓ | ✓ |
| Ver detalle SIM | `GET /v1/sims/{iccid}` | ✓ | ✓ | ✓ |
| Ver consumo | `GET /v1/sims/{iccid}/usage` | ✓ | ✓ | ✓ |
| Ver presencia | `GET /v1/sims/{iccid}/presence` | ✓ | ✓ | ✓ |
| Ver capabilities proveedor | `GET /v1/providers/{provider}/capabilities` | ✓ | ✓ | ✓ |
| Ver credenciales configuradas, sin secretos | `GET /v1/companies/me/credentials/**` | ✗ | ✓ | ✓ |
| Probar / crear / rotar credenciales propias | `POST/PATCH /v1/companies/me/credentials/**` | ✗ | ✓ | ✓ |
| Descubrir subcompañías Moabits | `GET /v1/companies/me/credentials/moabits/companies/discover` | ✗ | ✓ | ✓ |
| Seleccionar `company_codes` Moabits | `PUT /v1/companies/me/credentials/moabits/company-codes` | ✗ | ✗ | ✓ |
| Desactivar credencial activa | `DELETE /v1/companies/me/credentials/{provider}` | ✗ | ✗ | ✓ |
| Cambiar estado administrativo | `PUT /v1/sims/{iccid}/status` | ✗ | ✗ | ✓ |
| **Purge (control op)** | `POST /v1/sims/{iccid}/purge` | ✗ | ✗ | ✓ |

`public` (rol del usuario que se acaba de auto-registrar y no fue confirmado por un admin) **no tiene acceso** a ningún endpoint de subscriptions.

`manager` puede gestionar credenciales sólo bajo `companies/me`; no hay endpoint con `{company_id}` para que pueda operar otro tenant. Los secrets nunca se devuelven en responses. La selección de `company_codes` Moabits queda reservada a `admin` porque define el scope operativo de la fuente Moabits, aunque no sea un secreto.

### 4. Tenant scoping

- Cada request resuelve `company_id` del `Profile` autenticado.
- El service `SubscriptionFetcher` consulta `SimRoutingMap` filtrando por `company_id`. Si el `iccid` solicitado no pertenece al tenant → **`404 SubscriptionNotFound`**, **no 403**, para no filtrar la existencia del recurso a otros tenants.
- `admin` no puede operar sobre SIMs de otra `Company` por la API normal. Si en el futuro se necesita "super-admin global", crearlo como rol nuevo y endpoints separados.

### 5. Auditoría — tablas `audit_log` y `lifecycle_change_audit`

```sql
CREATE TABLE audit_log (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at   timestamptz NOT NULL DEFAULT now(),
    actor_id      uuid NOT NULL REFERENCES profiles(id),
    company_id    uuid NOT NULL REFERENCES companies(id),
   action        text NOT NULL,                 -- 'subscription.purge', 'credentials.rotate'
    target_type   text NOT NULL,                 -- 'subscription', 'credentials'
    target_id     text NOT NULL,                 -- iccid o credential_id
    request_id    text NOT NULL,                 -- correlación con logs
    outcome       text NOT NULL,                 -- 'success' | 'denied' | 'error'
    detail        jsonb                          -- campos relevantes (provider, technologies, error_code, etc.)
);

CREATE INDEX idx_audit_company_time ON audit_log (company_id, occurred_at DESC);
CREATE INDEX idx_audit_actor_time   ON audit_log (actor_id, occurred_at DESC);
```

El backend implementa además `lifecycle_change_audit` para los writes de SIM:

```sql
CREATE TABLE lifecycle_change_audit (
    id                  bigserial PRIMARY KEY,
    company_id          text,
    actor_id            text,
    request_id          text,
    iccid               text NOT NULL,
    provider            text NOT NULL,
    action              text NOT NULL,
    target              text,
    idempotency_key     text,
    requested_at        timestamptz NOT NULL DEFAULT now(),
    accepted_at         timestamptz,
    outcome             text NOT NULL DEFAULT 'unknown',
    latency_ms          integer,
    provider_request_id text,
    provider_error_code text,
    error               text,
    meta                jsonb
);
```

Reglas:
- **Toda mutación** (purge, rotación de credenciales, cambios de rol) escribe una fila.
- **Toda denegación 403** sobre mutaciones también se audita (`outcome='denied'`) — útil para detectar abuso.
- Retención: 1 año mínimo. Cron mensual purga > 1 año (configurable). `[REQUIRES INPUT: ¿hay requerimiento legal de retención?]`

### 6. Idempotencia de mutaciones

Por ADR-007: `Idempotency-Key` requerido en `POST /v1/sims/{iccid}/purge`. La tabla `idempotency_keys` retiene 24 h y usa unique `(company_id, key)` para permitir la misma key en compañías distintas. Sin la key, la API responde `400 IdempotencyKeyRequired`.

## Consecuencias

**Positivas**
- Cero migración de auth; el equipo conoce el código.
- Multi-tenant claro: `company_id` es el lente único.
- Mutaciones siempre auditadas.
- `member` puede usar la app sin riesgo de disparar acciones costosas.

**Negativas / mitigaciones**
- HS256 con secreto compartido es menos seguro que RS256 con clave asimétrica → **mitigación**: rotación documentada; migrar cuando haya tiempo.
- Sin SSO/SAML/OIDC → **aceptable** mientras la app sea interna. Si la empresa adopta SSO, ahí ADR-008 se reabre.
- Refresh tokens guardados en DB tienen que estar hasheados → trabajo de migración chico (forzar logout una vez).

## Alternativas consideradas

1. **Migrar a Supabase Auth nativo / Auth0**
   - Pros: SSO out-of-the-box, MFA, social login.
   - Contras: migración costosa hoy, dependencia adicional, no resuelve un problema que tengamos. Diferida.
   - **Rechazada por ahora**.

2. **API key por compañía**
   - Pros: simple para integraciones server-to-server.
   - Contras: incompatible con RBAC por usuario; no audita quién hizo qué.
   - **Rechazada**.

3. **Permisos por scope en JWT** (ej. `subs:read`, `subs:reset`)
   - Pros: granularidad fina.
   - Contras: cuatro roles ya cubren la matriz; agregar scopes ahora es over-engineering.
   - **Diferida**.

## Trade-offs explícitos

| Eje | JWT actual + RBAC (elegido) | IdP gestionado | API key |
|---|---|---|---|
| Esfuerzo migración | nulo | alto | medio |
| Auditoría por usuario | sí | sí | no |
| SSO | no | sí | no |
| Costo $/mes | nulo | medio | nulo |

## Cuándo revisar

- Adopción corporativa de SSO (OIDC/SAML) → migrar a IdP federado.
- Aparición de integraciones server-to-server (B2B) → introducir API keys con scopes específicos.
- Si auditoría legal exige firma WORM, mover `audit_log` a object storage WORM o append-only ledger.
