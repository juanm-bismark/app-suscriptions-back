# ADR-010 — Moabits: bootstrap explícito del company code, sin auto-scope por nombre

- **Estado**: Accepted
- **Fecha**: 2026-05-07
- **Decisores**: equipo backend
- **Relacionado**: ADR-003 (ACL/adapter), ADR-005 (resiliencia y caché), ADR-006 (credenciales cifradas), ADR-008 (RBAC)

## Contexto

Moabits (Orion) requiere acotar las consultas de SIMs a la subcompañía
hija asociada al `x-api-key`. El code operativo se persiste en
`CompanyProviderMapping` (una fila activa por tenant). Las credenciales
sensibles (`base_url`, `x_api_key`) viven cifradas en
`company_provider_credentials.credentials_enc`. El router combina ambas
fuentes antes de llamar al adapter: inyecta `company_code` (singular)
desde el mapping, y el adapter usa ese valor directamente en
`list_subscriptions` sin iterar sobre una lista.

Hasta esta versión, `app/subscriptions/routers/sims.py` exponía una
función `_auto_scope_moabits_credentials` que se ejecutaba **en cada
`GET /v1/sims?provider=moabits`** cuando no había company code configurado:

1. Hacía una request extra a `/api/company/childs/{parent}` para obtener
   las subcompañías visibles para el `x-api-key`.
2. Filtraba las que matcheaban por **nombre** con el `Company.name` local
   del tenant, usando una normalización Unicode + casefold y comparación
   sub-string (`local in provider or provider in local`).
3. Si encontraba uno o más matches, los inyectaba en memoria — **sin
   persistir** — y delegaba al adapter.
4. Si no encontraba match, lanzaba `412 ListingPreconditionFailed`.

Era un bootstrap pensado para que el primer listado funcionara sin
configuración previa, asumiendo que el nombre del tenant local
coincidiría con el `companyName` que devuelve Moabits.

### Problemas observados

- **Match por string es frágil**: cualquier renombre, abreviatura
  ("Bismark Colombia S.A.S." vs "Bismark Colombia"), tilde mal puesta o
  diferencia de mayúsculas en cualquiera de los dos lados rompe el match
  y hace que todo el listado caiga con 412.
- **Costo por request**: cada listado disparaba una llamada extra a
  Moabits (`/api/company/childs/{parent}`) además de las propias del
  listado. Latencia adicional ~100-300 ms y doble presión de cuota.
  Contradice el espíritu de ADR-005 (evitar llamadas upstream
  innecesarias).
- **No determinismo y no auditable**: la lista de codes usada en una
  request dependía del estado de Moabits *en ese instante*. No había
  forma de ver, leyendo la BD, qué codes iba a scopear el sistema en el
  próximo listado.
- **Acoplamiento implícito**: el campo `Company.name` (concebido como
  metadata local del tenant) quedaba acoplado al `companyName` de
  Moabits, sin que ningún contrato lo dijera. Un renombre rompía un flujo
  no relacionado.

## Decisión

Eliminar el auto-scope. El company code Moabits debe estar **persistido
explícitamente** en `company_provider_mappings.provider_company_code`
antes de poder listar. `moabits_source_companies` queda como cache de
discovery para la UI, no como fuente de autorización operativa. El flujo
de onboarding queda:

1. **Crear/rotar credencial**: `PATCH /v1/companies/me/credentials/moabits`
   con `base_url` y `x_api_key`. No se guarda ningún company code dentro
   del blob cifrado de credenciales — el parent code (`48123`) es una
   constante del adapter (`MOABITS_PARENT_COMPANY_CODE`).
2. **Descubrir subcompañías**:
   `GET /v1/companies/provider-mappings/moabits/discover`
   consulta Moabits, refresca `moabits_source_companies` y devuelve las
   subcompañías visibles para que el admin escoja.
3. **Persistir selección**:
   `PUT /v1/companies/{company_id}/provider-mappings/moabits` con el
   `provider_company_code` final. Valida contra Moabits/cache que el code
   exista y lo guarda en `company_provider_mappings`.
4. **Listar SIMs**: `GET /v1/sims?provider=moabits` lee directamente
   `company_provider_mappings.provider_company_code`, lo inyecta en las
   credenciales resueltas para esa llamada y delega al adapter. Sin
   llamadas extra de discovery.

El `PUT /v1/companies/{company_id}/provider-mappings/moabits` es
**admin-only**. Aunque no persiste secretos, cambia el scope efectivo del
origen Moabits y por tanto qué SIMs entran al listado operativo. Esa
decisión se trata como configuración de fuente, no como rotación ordinaria
de credenciales delegable a `manager`.

Si el mapping falta al listar, el router responde
`412 ListingPreconditionFailed` con un mensaje accionable que apunta a
los pasos 2 y 3:

```
"Moabits credentials have no company mapping configured.
 Call GET /v1/companies/provider-mappings/moabits/discover to list
 available companies, then PUT /v1/companies/{company_id}/provider-mappings/moabits
 to persist the selection."
```

## Consecuencias

**Positivas**
- Listado de SIMs Moabits hace **una** request a Moabits en lugar de dos.
- Comportamiento determinístico y auditable: el code efectivo
  es exactamente lo que la BD dice — `SELECT ... FROM
  company_provider_mappings WHERE provider = 'moabits'` es la fuente de
  verdad.
- Desaparece el acoplamiento implícito `Company.name` ↔ `companyName`
  Moabits. Renombrar el tenant local no rompe el listado.
- El error de configuración aparece en el primer listado con un mensaje
  claro y accionable, no como un 412 críptico ("no Moabits child company
  matched the current company name").
- Código más simple: 4 helpers eliminados de `sims.py` (`_normalize_match_text`,
  `_company_name_matches`, `_company_name`, `_auto_scope_moabits_credentials`),
  además de imports muertos (`unicodedata`, `fetch_child_companies`,
  `Company`).

**Negativas / mitigaciones**
- **Onboarding requiere un paso explícito**: el primer listado no
  funciona sin antes haber persistido el mapping.
  - **Mitigación**: el endpoint `/companies/discover` ya existe para que
    el frontend muestre la lista al usuario y haga el `PUT` con la
    selección. Es 1 click adicional al setup, una sola vez por fuente.
- **Sólo admin puede persistir la selección**: sólo un `admin` puede
  cambiar el scope efectivo.
  - **Mitigación**: esto evita ampliar o reducir el universo operativo de
    SIMs sin una persona con permiso administrativo. La credencial se
    puede crear/rotar por `manager`, pero el alcance final de Moabits
    queda bajo control de `admin`.
- **No se autodescubren nuevas subcompañías**: si Moabits agrega una
  child company después del setup, no aparece automáticamente en el
  listado.
  - **Mitigación**: `/companies/discover` es read-only y barato; el
    frontend puede ofrecer "actualizar selección" cuando sea necesario.
    Esto es lo correcto: una nueva subcompañía debería ser una decisión
    explícita del operador, no un cambio silencioso.

## Alternativas consideradas

1. **Mantener el auto-scope tal cual**
   - Pros: cero cambios; UX cero-config.
   - Contras: todos los problemas listados en *Problemas observados*.
   - **Rechazada**.

2. **Auto-scope con write-through**: hacer el match por nombre la primera
   vez y *persistir* el resultado en `credentials_enc` para no
   re-descubrir.
   - Pros: bootstrap automático; segunda llamada en adelante es directa.
   - Contras: el match por nombre sigue siendo frágil; persistir
     resultados de un heurístico opaco hace el bug más difícil de
     diagnosticar (queda guardado en BD); requiere lógica para invalidar
     el caché si el nombre cambia.
   - **Rechazada**: cambia el síntoma pero no el problema. El acoplamiento
     `Company.name ↔ companyName` sigue ahí, solo más enterrado.

3. **Hacer la selección parte obligatoria del PATCH inicial de
   credenciales**: rechazar el PATCH si `credentials.company_code` está
   vacío.
   - Pros: imposible quedar en estado intermedio.
   - Contras: el operador puede no conocer los codes al momento de
     pegar la `x_api_key`. Forzar el conocimiento en ese paso obliga a
     consultar Moabits manualmente antes de poder guardar la credencial.
   - **Rechazada**: el flujo discover + PUT es más amigable y mantiene la
     misma garantía operativa (no se puede listar sin codes).

## Trade-offs explícitos

| Eje | Bootstrap explícito (elegido) | Auto-scope por nombre |
|---|---|---|
| Llamadas upstream por listado | 1 (solo el listado) | 2 (discover + listado) |
| Determinismo | Alto (BD = verdad) | Bajo (depende de runtime) |
| Auditabilidad | `SELECT company_provider_mappings` muestra todo | Hay que reproducir el match |
| Robustez ante renombres | Inmune | Frágil |
| Pasos de onboarding | discover + PUT (1 click) | Cero, si los nombres matchean |
| Diagnóstico de fallos | Mensaje accionable directo | "no match" críptico |

## Cuándo revisar

- Si en el futuro Moabits expone un endpoint que devuelve directamente
  "todas las companies que este x-api-key controla" sin requerir
  iteración por code, podría considerarse re-introducir un auto-scope
  trivial (sin match por nombre) que persista la lista completa al primer
  listado. No aplica hoy: `simListByCompany` exige iterar por code.
- Si aparece un caso de uso multi-tenant donde un operador legítimamente
  cambia su selección de companies con frecuencia (ej. clientes que
  rotan), evaluar UX del frontend para no requerir re-confirmación
  manual cada vez.

## Implementación

Cambios en este ADR:
- `app/subscriptions/routers/sims.py`: eliminados `_normalize_match_text`,
  `_company_name_matches`, `_company_name`, `_auto_scope_moabits_credentials`
  e imports `unicodedata`, `fetch_child_companies`, `Company`. Nueva
  función `_require_moabits_company_mapping` que lanza
  `ListingPreconditionFailed` si el mapping está vacío.
- `tests/test_sims_router_controls.py`: reemplazado el test
  `test_moabits_listing_auto_scopes_company_code_from_name_match` por
  dos tests:
  - `test_moabits_listing_requires_persisted_company_mapping` (412 cuando
    falta el mapping).
  - `test_moabits_listing_uses_persisted_company_mapping` (listado normal
    cuando ya está guardado; el adapter no recibe llamadas de
    discovery extra).
- `app/tenancy/routers/companies.py`: `PUT
  /v1/companies/{company_id}/provider-mappings/moabits` usa `AdminProfile`,
  valida el code contra Moabits/cache y persiste la selección en
  `company_provider_mappings`.
- `tests/test_credentials_router.py`: agregado
  `test_only_admin_can_select_moabits_mapping` para asegurar `403`
  a `manager` y cero commits.

Migraciones de BD: `006_moabits_source_companies.sql` y
`007_company_provider_mappings.sql`. El company code deja de formar parte
del JSON cifrado de credenciales y pasa a ser un mapping no secreto por
Company local.
