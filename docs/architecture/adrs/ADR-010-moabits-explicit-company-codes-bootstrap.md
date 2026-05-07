# ADR-010 — Moabits: bootstrap explícito de `company_codes`, sin auto-scope por nombre

- **Estado**: Accepted
- **Fecha**: 2026-05-07
- **Decisores**: equipo backend
- **Relacionado**: ADR-003 (ACL/adapter), ADR-005 (resiliencia y caché), ADR-006 (credenciales cifradas)

## Contexto

Moabits (Orion) requiere acotar las consultas de SIMs por una lista de
`company_codes` (subcompañías hijas asociadas al `x-api-key`). Esa lista
se persiste como secreto en `company_provider_credentials.credentials_enc`
junto con `base_url` y `x_api_key`. El adapter usa ese campo en
`list_subscriptions` para iterar por code.

Hasta esta versión, `app/subscriptions/routers/sims.py` exponía una
función `_auto_scope_moabits_credentials` que se ejecutaba **en cada
`GET /v1/sims?provider=moabits`** cuando `company_codes` estaba vacío:

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

Eliminar el auto-scope. `company_codes` Moabits debe estar **persistido
explícitamente** en `company_provider_credentials.credentials_enc` antes
de poder listar. El flujo de onboarding queda:

1. **Crear/rotar credencial**: `PATCH /v1/companies/me/credentials/moabits`
   con `base_url`, `x_api_key`, y opcionalmente `company_codes` iniciales.
2. **Descubrir subcompañías** (read-only):
   `GET /v1/companies/me/credentials/moabits/companies/discover`
   devuelve la lista de subcompañías visibles para el `x-api-key`,
   marcando cuáles ya están seleccionadas. **No escribe en BD.**
3. **Persistir selección**:
   `PUT /v1/companies/me/credentials/moabits/company-codes` con la lista
   final. Valida contra Moabits que todos los codes existan y los guarda
   cifrados.
4. **Listar SIMs**: `GET /v1/sims?provider=moabits` lee directamente
   `credentials.company_codes` de BD y delega al adapter. Sin llamadas
   extra de discovery.

Si `company_codes` está vacío al listar, el router responde
`412 ListingPreconditionFailed` con un mensaje accionable que apunta a
los pasos 2 y 3:

```
"Moabits credentials have no company_codes configured.
 Call GET /v1/companies/me/credentials/moabits/companies/discover to list
 available companies, then PUT /v1/companies/me/credentials/moabits/company-codes
 to persist the selection."
```

## Consecuencias

**Positivas**
- Listado de SIMs Moabits hace **una** request a Moabits en lugar de dos.
- Comportamiento determinístico y auditable: la lista efectiva de codes
  es exactamente lo que la BD dice — `SELECT ... FROM
  company_provider_credentials` es la fuente de verdad.
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
  funciona sin antes haber persistido `company_codes`.
  - **Mitigación**: el endpoint `/companies/discover` ya existe para que
    el frontend muestre la lista al usuario y haga el `PUT` con la
    selección. Es 1 click adicional al setup, una sola vez por tenant.
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
   credenciales**: rechazar el PATCH si `credentials.company_codes` está
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
| Auditabilidad | `SELECT credentials_enc` muestra todo | Hay que reproducir el match |
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
  función `_require_moabits_company_codes` que lanza
  `ListingPreconditionFailed` si `company_codes` está vacío.
- `tests/test_sims_router_controls.py`: reemplazado el test
  `test_moabits_listing_auto_scopes_company_code_from_name_match` por
  dos tests:
  - `test_moabits_listing_requires_persisted_company_codes` (412 cuando
    falta `company_codes`).
  - `test_moabits_listing_uses_persisted_company_codes` (listado normal
    cuando ya están guardados; el adapter no recibe llamadas de
    discovery extra).

Sin migraciones de BD. El campo `company_codes` ya existía en el JSON
cifrado y es la única fuente de verdad ahora.
