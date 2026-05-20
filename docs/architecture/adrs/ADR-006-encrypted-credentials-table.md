# ADR-006 — Credenciales por tenant en tabla cifrada (no en `company_settings.settings`)

- **Estado**: Accepted
- **Fecha**: 2026-04-22
- **Decisores**: equipo backend + lead seguridad
- **Relacionado**: ADR-008 (auth + RBAC)

## Contexto

Cada `Company` (tenant) puede operar con uno o más proveedores. Para cada (`Company`, `Provider`) hace falta guardar:
- Credenciales (token, user/pass, certificado).
- Scope de cuenta del proveedor (Kite `endCustomerId`, Tele2 `accountId`; Moabits usa solo `x_api_key` cifrada — el código de compañía operativo vive en `CompanyProviderMapping`, no en las credenciales).
- Estado (activa/inactiva).
- Auditoría de rotación.

Existe ya una tabla `company_settings(company_id, settings JSONB)`. La tentación inmediata es guardar las credenciales ahí. Es **mala idea**:
- Mezcla configuración (no sensible) con secretos.
- Cualquier query a `settings` los expone.
- Sin auditoría dedicada de rotación.
- Sin esquema → drift garantizado entre proveedores.

## Decisión

### 1. Tabla dedicada

```sql
CREATE TABLE company_provider_credentials (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    provider        text NOT NULL,
    credentials_enc text NOT NULL,                 -- Fernet token
    account_scope   jsonb NOT NULL DEFAULT '{}',   -- NO sensible: endCustomerId, accountId, cert_expires_at, environment
    active          boolean NOT NULL DEFAULT true,
    rotated_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX company_provider_credentials_active_idx
    ON company_provider_credentials (company_id, provider)
    WHERE active = TRUE;
```

### 2. Cifrado en aplicación con Fernet

- Clave maestra `FERNET_KEY` (32 bytes URL-safe base64, generada con `Fernet.generate_key()`) en variable de entorno gestionada por el orquestador (Docker secrets / Vault / variables PaaS). **Nunca** en repo.
- Librería: `cryptography.Fernet`.
- Estructura cifrada (antes de cifrar): `{"username": "...", "password": "...", "token": "..."}` — JSON serializado, sólo los campos que aplican al provider.
- Kite PFX se guarda en este JSON como `client_cert_pfx_b64` y `client_cert_password`.
- `account_scope` queda sólo para metadata no secreta: `environment`, `end_customer_id`, `account_id`, `cert_expires_at`. Para Moabits, `company_codes` vive en `provider_source_configs.settings.company_codes`, no en `account_scope`.

### 3. Resolución de credenciales

```python
class CredentialResolver:
    @cached(ttl=60)
    async def resolve(self, company_id: UUID, provider: Provider) -> ProviderCredentials: ...
```

Diseño objetivo: TTL = 60 s e invalidación explícita al rotar. Estado
actual: el router usa helpers que leen, desencriptan y enriquecen las
credenciales por request; no hay caché TTL de credenciales implementado.

### 4. Gestión y rotación

- Endpoints sobre el tenant autenticado, nunca sobre un `{company_id}` arbitrario:
  - `GET /v1/companies/me/credentials` — `manager` y `admin`, sólo metadata.
  - `GET /v1/companies/me/credentials/{provider}` — `manager` y `admin`, sólo metadata.
  - `POST /v1/companies/me/credentials/{provider}/test` — `manager` y `admin`, valida conectividad sin persistir secretos nuevos.
  - `PATCH /v1/companies/me/credentials/{provider}` — `manager` y `admin`, crea o rota credenciales propias del tenant.
  - `GET /v1/companies/me/credentials/moabits/companies/discover` — `manager` y `admin`, descubre subcompañías visibles sin persistir cambios.
  - `PUT /v1/companies/me/credentials/moabits/company-codes` — `admin` only, persiste el scope efectivo de Moabits en `provider_source_configs`.
  - `DELETE /v1/companies/me/credentials/{provider}` — `admin` only, desactiva la credencial activa.
- La rotación (`PATCH`) actualmente:
  1. Marca el registro activo como `active = false`.
  2. Inserta nuevo registro con `active = true`.
  3. Prueba la credencial antes de persistir.
  4. Devuelve metadata sin secretos.

Pendiente: escribir una fila genérica en `audit_log` para rotación/desactivación de credenciales e invalidar caché cuando exista.
- Mantener registros viejos `inactive` 30 días para forensics, luego cron de purga.
- `manager` puede gestionar credenciales **sólo de su propia Company** porque el endpoint usa `companies/me`. No puede elegir otro `company_id`, cambiar roles, ni ejecutar writes destructivos de SIM (`purge`, cambios de estado).
- Para Moabits, `company_codes` no se considera secreto y no vive en `credentials_enc`: se guarda en `provider_source_configs.settings.company_codes` como configuración no sensible de la fuente. Cambiarlo sí queda restringido a `admin`, porque altera qué subcompañías/SIMs participan en el listado operativo.
- `PATCH` y `DELETE` no requieren `Idempotency-Key` en el código actual; la idempotencia obligatoria está implementada para writes de SIM (`PUT /v1/sims/{iccid}/status`, `POST /v1/sims/{iccid}/purge`). `POST /test` no persiste secretos.

### 5. Lo que **NO** se hace

- Las credenciales **no** se loguean nunca (ni en debug). El logger tiene un scrubber configurado con la lista negra de campos.
- La API **no** devuelve nunca las credenciales en una response (ni siquiera al admin que las acaba de subir). Se devuelve sólo metadata (`active`, `rotated_at`).
- `company_settings.settings` queda explícitamente reservado para configuración no sensible (preferencias UI, flags por tenant). Documentado en su modelo.

## Consecuencias

**Positivas**
- Aislamiento de blast radius: un dump de `company_settings` no compromete proveedores externos.
- Auditoría de rotación nativa.
- Esquema explícito por `account_scope` (visible) y `credentials` (cifrado opaco).
- Migrar a un KMS gestionado (AWS KMS / GCP Secret Manager / Vault) en el futuro = cambiar **sólo** la implementación de cifrado, no el modelo.

**Negativas / mitigaciones**
- La clave maestra es un single-point-of-compromise → **mitigación**: gestión por orquestador, rotación documentada de la master key (re-encrypt en bloque), nunca en repo, scrubber en logs.
- Rotación manual hasta que se integre KMS → aceptable a la escala actual.

## Alternativas consideradas

1. **Guardar en `company_settings.settings` JSONB**
   - Pros: cero schema work.
   - Contras: tabla de configuración con secretos, sin auditoría, sin cifrado, expuesta a cualquier `SELECT settings`.
   - **Rechazada**.

2. **Vault / AWS Secrets Manager / GCP Secret Manager dedicado desde el día 1**
   - Pros: gestión profesional.
   - Contras: dependencia operativa nueva, costo de setup, overkill para 3 proveedores × pocas compañías. **Diferida** — el modelo elegido permite migrar sin tocar el dominio.
   - **Diferida**.

3. **`pgcrypto` con `pgp_sym_encrypt` (cifrado en DB)**
   - Pros: cifrado nativo Postgres.
   - Contras: clave en DB (mismo trust boundary), DBA puede leerlas, dificulta rotar.
   - **Rechazada**.

## Trade-offs explícitos

| Eje | Tabla cifrada (elegido) | JSONB de settings | KMS gestionado |
|---|---|---|---|
| Aislamiento | alto | nulo | máximo |
| Esfuerzo inicial | medio | trivial | alto |
| Costo | nulo | nulo | $$ |
| Auditoría | nativa | manual | nativa |
| Migración futura a KMS | trivial | refactor | n/a (ya está) |

## Cuándo revisar

- Adopción corporativa de Vault/KMS → migrar implementación de cifrado, mantener tabla.
- Los certificados PFX de Kite deben guardarse como base64 dentro de `credentials_enc` mientras el tamaño siga siendo razonable para Postgres. No van en `.env` ni en `account_scope`. Si aparecen credenciales de varios MB o rotación/firma compleja, mover el blob a object storage cifrado y guardar sólo la referencia.
- Para Kite, `account_scope` contiene la metadata no secreta: `end_customer_id`, `environment` y `cert_expires_at`. Estos campos no se copian a `credentials_enc`; el adapter de Kite no los usa en llamadas SOAP. `client_cert_pfx_b64`, `client_cert_password`, `username` y `password` viven cifrados en `credentials_enc`.
