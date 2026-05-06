Actualización basada en Swagger Orion API 2.0.0 (`https://www.api.myorion.co/api-doc`).

- Server declarado: `https://www.api.myorion.co/`. No se declara sandbox separado.
- Alcance acordado para backend v1: endpoints `GET`, operaciones admin `PUT /api/sim/active/` y `PUT /api/sim/suspend/`, y la excepción de escritura `PUT /api/sim/purge/`.
- Autorización: `GET /integrity/authorization-token` con header `x-api-key`; retorna un JWT que debe usarse como `Authorization: Bearer <authorizationToken>`.
- Solo se incluyen operaciones definidas explícitamente en el documento fuente.
- El endpoint `GET /api/v2/product/product-list` no está definido en este Swagger. Los productos asociados a una compañía aparecen embebidos dentro de la respuesta de `getCompanyInfo` en el campo `products[]`; por tanto, se mapean como parte de ese esquema y no como operación independiente.
- Los endpoints de listado de SIMs (`/api/company/simList/{companyCodes}` y `/api/company/simListDetail/{companyCodes}`) documentan únicamente el parámetro path `companyCodes`. No hay filtros públicos `modified_since`, `modified_till`, `modifiedSince`, `modifiedTill`, `startLastStateChangeDate`, ni equivalentes en el Swagger de Moabits/Orion 2.0.0.

## 0) Autorización

### Endpoint / Operación: `getAuthorizationToken`

- Ruta: `GET /integrity/authorization-token`
- Propósito: Obtener un JWT necesario para llamar cualquier otro endpoint de Orion API. Expira en 6 horas.
- Parámetros de entrada: `x-api-key` (header, string, requerido), application key entregada por Orion Web Client.
- Esquema de respuesta: `status`; objeto `info` con `authorizationToken` (JWT).
- Política de backend:
  - El adapter decodifica el JWT sin verificar firma para leer `exp` y cachearlo localmente.
  - Refresca proactivamente cuando faltan menos de 5 minutos para `exp`.
  - Si un endpoint de negocio responde 401, refresca el JWT una vez y reintenta la petición original una sola vez.
  - Si `/integrity/authorization-token` responde 401, se trata como `x-api-key` ausente, malformada o inexistente.
  - Si `/integrity/authorization-token` responde 403, se trata como `x-api-key` cancelada, revocada o expirada; requiere rotación de credencial.
  - Si un endpoint de negocio responde 403, se trata como falta de permisos sobre el recurso, no como expiración del JWT.

## 1) Suscripciones

### Endpoint / Operación: `getCompanySimList`

- Ruta: `GET /api/company/simList/{companyCodes}`
- Propósito: Listar todas las SIMs y su estatus pertenecientes a uno o más códigos de compañía.
- Parámetros de entrada: `companyCodes` (path, `array<string>`, requerido).
- Esquema de respuesta: `status`; objeto `info` con `iccidList[]`, donde cada elemento contiene:
  - `iccid`
  - `simStatus` (`Active` | `Ready` | `Suspended`)
  - `dataService` (`Enabled` | `Disabled`)
  - `smsService` (`Enabled` | `Disabled`)

### Endpoint / Operación: `getCompanySimListDetail`

- Ruta: `GET /api/company/simListDetail/{companyCodes}`
- Propósito: Obtener el detalle administrativo y técnico de las SIMs por lista de códigos de compañía.
- Parámetros de entrada: `companyCodes` (path, `array<string>`, requerido).
- Esquema de respuesta: `status`; objeto `info` con `simInfo[]`, donde cada elemento incluye:
  - `iccid`
  - `lastNetwork`
  - `first_lu`
  - `first_cdr`
  - `last_lu`
  - `last_cdr`
  - `firstcdrmonth`
  - `imei`
  - `autorenewal`
  - `product_name`
  - `product_code`
  - `product_id`
  - `clientName`
  - `companyCode`
  - `dataLimit`
  - `smsLimit`
  - `services`
  - `imsi`
  - `imsiNumber`
  - `msisdn`
  - `numberOfRenewalsPlan`
  - `remainingRenewalsPlan`
  - `planStartDate`
  - `planExpirationDate`

### Endpoint / Operación: `getSimDetails`

- Ruta: `GET /api/sim/details/{iccidList}`
- Propósito: Recuperar el detalle administrativo/técnico de SIMs a partir de una lista de ICCIDs.
- Parámetros de entrada: `iccidList` (path, `array<string>`, requerido, máximo 50 por petición).
- Esquema de respuesta: `status`; objeto `info` con `simInfo[]`, con los mismos campos documentados para `simListDetail`.

## 2) Estatus

### Endpoint / Operación: `getSimServiceStatus`

- Ruta: `GET /api/sim/serviceStatus/{iccidList}`
- Propósito: Obtener el estatus administrativo de servicios (SIM, datos, SMS) para una lista de ICCIDs.
- Parámetros de entrada: `iccidList` (path, `array<string>`, requerido).
- Esquema de respuesta: `status`; objeto `info` con `iccidList[]`, donde cada elemento contiene:
  - `iccid`
  - `simStatus` (`Active` | `Ready` | `Suspended`)
  - `dataService` (`Enabled` | `Disabled`)
  - `smsService` (`Enabled` | `Disabled`)

### Endpoint / Operación: `getSimConnectivityStatus`

- Ruta: `GET /api/sim/connectivityStatus/{iccidList}`
- Propósito: Obtener el estatus de conectividad en red (`Online` / `Offline`) de una lista de SIMs.
- Parámetros de entrada: `iccidList` (path, `array<string>`, requerido).
- Esquema de respuesta: `status`; objeto `info` con `connectivityStatus[]`, donde cada elemento contiene:
  - `iccid`
  - `status` (`Online` | `Offline`)
  - `country` (país de la red conectada)
  - `rat` (Radio Access Technology, por ejemplo `4G` / `3G`)
  - `network` (nombre de la red)

## 3) Consumo

### Endpoint / Operación: `getSimUsage`

- Ruta: `GET /api/usage/simUsage`
- Propósito: Obtener el consumo (datos y SMS) de una lista de SIMs en un rango de fechas.
- Parámetros de entrada:
  - `iccidList` (query, `array<string>`, requerido, máximo 50)
  - `initialDate` (query, string `yyyy-MM-dd HH:mm:ss`, requerido)
  - `finalDate` (query, string `yyyy-MM-dd HH:mm:ss`, requerido)
- Restricción adicional: rango máximo de 6 meses.
- Esquema de respuesta: `status`; objeto `info` con `simsUsage[]`, donde cada elemento contiene:
  - `iccid`
  - `activeSim` (boolean)
  - `smsMO` (outgoing)
  - `smsMT` (incoming)
  - `data` (MB totales en el rango)

### Endpoint / Operación: `getCompanyUsage`

- Ruta: `GET /api/usage/companyUsage`
- Propósito: Obtener el consumo agregado por código de compañía en un rango de fechas.
- Parámetros de entrada:
  - `companyCodes` (query, `array<string>`, requerido, máximo 3)
  - `initialDate` (query, string `yyyy-MM-dd HH:mm:ss`, requerido)
  - `finalDate` (query, string `yyyy-MM-dd HH:mm:ss`, requerido)
- Restricción adicional: rango máximo de 6 meses.
- Esquema de respuesta: `status`; objeto `info` con `companyUsage[]`, donde cada elemento contiene:
  - `code`
  - `name`
  - `activeSims`
  - `smsMO`
  - `smsMT`
  - `data` (MB totales en el rango)

### Endpoint / Operación: `getSmsHistory`

- Propósito: Recuperar el histórico de SMS (hasta 3 meses).
- Parámetros de entrada:
  - `initialDate` (query, string `yyyy-MM-dd HH:mm:ss`, requerido)
  - `finalDate` (query, string `yyyy-MM-dd HH:mm:ss`, requerido)
- Esquema de respuesta: `status`; `SMSList[]`, donde cada elemento contiene:
  - `iccids[]`
  - `date`
  - `message`
  - `smsType` (`SMS MO` | `SMS MT`)
  - `SMSGWDELIVERY` (boolean, solo en `SMS MT`)
  - `SMSCDELIVERY` (boolean, solo en `SMS MT`)

## 4) Detalles Administrativos

### Endpoint / Operación: `getCompanyInfo`

- Propósito: Recuperar la información administrativa, comercial y de productos asociada a uno o más códigos de compañía. Incluye productos asociados, como mapeo del `product-list` solicitado.
- Parámetros de entrada: `companyCodes` (path, `array<string>`, requerido).
- Esquema de respuesta: `status`; objeto `info` con `companyinfo[]`, donde cada elemento contiene:
  - `client`
  - `clie_id`
  - `clientName`
  - `clientType`
  - `currency`
  - `country`
  - `contact`
  - `email`
  - `phonenumber`
  - `vat`
  - `companyRegistrationNumber`
  - `accountStatus`
  - `status`
  - `paymentMethod`
  - `exchangeRate`
  - `mblimit`
  - `smslimit`
  - `products[]` con objetos `{ id, name }`

### Endpoint / Operación: `getCompanyChilds`

- Propósito: Recuperar las compañías hijas (jerarquía) a partir de un código de compañía/token.
- Parámetros de entrada: `companyCode` (path, requerido).
- Esquema de respuesta: `status`; objeto `info` con `companyChilds[]`, donde cada elemento contiene:
  - `companyCode`
  - `companyName`

### Endpoint / Operación: `getClientType`

- Propósito: Listar todos los tipos de cliente activos en Orion.
- Parámetros de entrada: no requiere parámetros.
- Esquema de respuesta: `status`; objeto `info` con `clientTypes[]`, donde cada elemento contiene:
  - `id`
  - `name`

### Endpoint / Operación: `getUsersByCompany`

- Propósito: Recuperar los usuarios pertenecientes a una compañía dado su código.
- Parámetros de entrada: `companyCode` (path, requerido).
- Esquema de respuesta: `status`; objeto `info` con `users[]`, donde cada elemento contiene:
  - `userId`
  - `email`
  - `name`
  - `status`
  - `roles[]` (array de IDs de rol)
  - `company_name`
  - `creator`
  - `creationdate`

### Endpoint / Operación: `getUserDetails`

- Propósito: Recuperar la información detallada de un usuario por su ID.
- Parámetros de entrada: `userId` (path, requerido).
- Esquema de respuesta: `status`; objeto `info` con:
  - `user_id`
  - `email`
  - `name`
  - `status`
  - `roles[]`
  - `company_name`
  - `creator`
  - `creationdate`

## 5) Límites

### Endpoint / Operación: `getCompanyLimits`

- Propósito: Recuperar los límites de consumo (datos/SMS) y el umbral de alerta configurados a nivel de compañía.
- Parámetros de entrada: `companyCodes` (path, `array<string>`, requerido).
- Esquema de respuesta: `status`; objeto `info` con `companyLimitsInfo[]`, donde cada elemento contiene:
  - `companyCode`
  - `companyName`
  - `dataLimitMb`
  - `smsLimit`
  - `threshold` (porcentaje de uso que dispara la alerta)
  - `email` (correo notificado al alcanzar los límites)

Notas:

- Los límites a nivel de SIM individual (`dataLimit`, `smsLimit`) no tienen endpoint propio de consulta y viajan embebidos dentro de la respuesta de `getSimDetails` y `getCompanySimListDetail`.
- Los límites a nivel de compañía también aparecen replicados como `mblimit` y `smslimit` dentro del payload de `getCompanyInfo`.

## 6) Escrituras Admin / Purga / Network Reset

### Endpoint / Operación: `activeSims`

- Ruta: `PUT /api/sim/active/`
- Propósito: Activar los servicios (datos y/o SMS) de una lista de SIMs identificadas por ICCID. Es la contraparte de escritura de `getSimServiceStatus`: cambia `dataService` / `smsService` a `Enabled` y, en consecuencia, `simStatus` a `Active`.
- Alcance backend v1: solo administradores mediante `PUT /v1/sims/{iccid}/status` con `target=active`.
- Parámetros de entrada: body `application/json` con:
  - `iccidList` (`array<string>`, requerido)
  - `dataService` (boolean, requerido), `true` para activar servicio de datos
  - `smsService` (boolean, requerido), `true` para activar servicio de SMS
- Regla de negocio: al menos uno de `dataService` o `smsService` debe enviarse en `true`; de lo contrario Orion responde 400 (`No service to active`).
- Esquema de respuesta: `204 No Content` en éxito, sin payload.
- Errores documentados: 400 (sin `iccidList`, sin booleanos de servicio, o sin servicio a activar), 401 (Absent authorization), 403 (Access denied), 500 (Server error - client not found).

### Endpoint / Operación: `suspendSims`

- Ruta: `PUT /api/sim/suspend/`
- Propósito: Suspender los servicios (datos y/o SMS) de una lista de SIMs identificadas por ICCID. Es la contraparte de escritura de `getSimServiceStatus`: cambia `dataService` / `smsService` a `Disabled` y, en consecuencia, `simStatus` a `Suspended`.
- Alcance backend v1: solo administradores mediante `PUT /v1/sims/{iccid}/status` con `target=suspended`.
- Parámetros de entrada: body `application/json` con:
  - `iccidList` (`array<string>`, requerido)
  - `dataService` (boolean, requerido), `true` para suspender servicio de datos
  - `smsService` (boolean, requerido), `true` para suspender servicio de SMS
- Regla de negocio: al menos uno de `dataService` o `smsService` debe enviarse en `true`; de lo contrario Orion responde 400 (`No service to suspend`).
- Esquema de respuesta: `204 No Content` en éxito, sin payload.
- Errores documentados: 400 (sin `iccidList`, sin booleanos de servicio, o sin servicio a suspender), 401 (Absent authorization), 403 (Access denied), 500 (Server error - client not found).

### Endpoint / Operación: `purgeSims`

- Ruta: `PUT /api/sim/purge/`
- Propósito: Purgar (sacar de la red) una lista de SIMs identificadas por ICCID.
- Parámetros de entrada: body `application/json` con `iccidList` (`array<string>`, requerido).
- Ejemplo de body:

```json
{
  "iccidList": ["8910300000000......"]
}
```

- Esquema de respuesta: `status`; objeto `info` con `purged` (boolean), que indica el resultado de la operación.
- Ejemplo de éxito:

```json
{
  "status": "Ok",
  "info": {
    "purged": true
  }
}
```

## Observaciones Finales

- Los datos de consumo/uso (`smsMO`, `smsMT`, `data`, `activeSims`, `activeSim`) solo se exponen vía `getSimUsage` y `getCompanyUsage`; no aparecen en los endpoints de detalle/listado.
- Los límites aparecen en tres niveles: compañía (`getCompanyLimits`, `getCompanyInfo`) y SIM (embebidos en `getSimDetails` y `getCompanySimListDetail`). No existe un endpoint propio para límites por SIM.
- No hay un endpoint público `getSubscriptions` ni `getStatusDetail` con esos nombres exactos en el Swagger; las suscripciones se modelan vía los endpoints de listado/detalle de SIM y el estatus vía `getSimServiceStatus`.
- `trafficCut` / `consumptionDaily` no aparecen como objetos en el Swagger fuente; no se incluyen para evitar inventar campos no documentados.
- No hay filtros públicos de listado por fecha de modificación/cambio. Si Moabits expone en el futuro un filtro equivalente a `modified_since` / `modified_till`, deberá mapearse como filtro normalizado en el adapter; si solo expone algo semánticamente distinto como `last_cdr`, debería evaluarse como otro filtro normalizado separado, por ejemplo `last_activity_since`.
- Las escrituras Moabits de backend v1 quedan restringidas a administradores: `activeSims`, `suspendSims` y `purgeSims`. Las demás operaciones `PUT` del Swagger quedan fuera de contrato.
