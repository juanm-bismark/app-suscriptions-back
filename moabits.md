Actualización basada en Swagger Orion API 2.0.0 (`https://www.api.myorion.co/api-doc`):

- Server declarado: `https://www.api.myorion.co/`. No se declara sandbox separado.
- Autorización: `Authorization: Bearer <api_key/JWT>`. El Swagger expone `GET /integrity/authorization-token`, que retorna un JWT.
- Rutas oficiales confirmadas:
  - `GET /api/sim/details/{iccidList}`
  - `GET /api/sim/serviceStatus/{iccidList}`
  - `GET /api/usage/simUsage`
  - `GET /api/usage/companyUsage`
  - `GET /api/company/simList/{companyCodes}`
  - `GET /api/company/simListDetail/{companyCodes}`
  - `GET /api/sim/connectivityStatus/{iccidList}`
  - `PUT /api/sim/active/`
  - `PUT /api/sim/suspend/`
  - `PUT /api/sim/purge/`
  - `PUT /api/sim/setLimits/`
  - `PUT /api/sim/details/{iccid}/name/`
- Purga oficial: `PUT /api/sim/purge/` con body `{"iccidList":["8910300000000......"]}`. Respuesta de éxito: `200 {"status":"Ok","info":{"purged":true}}`.
- La purga no se hace con un body genérico `{status:"PURGED"}`; ese endpoint/campo no existe en el Swagger público.
- Transiciones escribibles en el API público: active, suspend, purge. No hay endpoint público para escribir `TEST_READY`, `DEACTIVATED` ni `INVENTORY`.

A continuación se presenta la especificación de requerimientos de los endpoints, extraída exclusivamente del documento fuente, organizada en las 6 secciones solicitadas.

SECCIÓN 1: SUSCRIPCIONES (Obtener líneas y su información básica)
Endpoint / Operación: Search Devices
Propósito: Buscar y obtener un listado de dispositivos (líneas) para una cuenta específica, permitiendo filtrar por estado o por campos personalizados.
Ruta confirmada: GET /api/company/simList/{companyCodes} y GET /api/company/simListDetail/{companyCodes}.
Parámetros de Entrada (Query/Path/Body): Cuenta específica (companyCodes), estado (status) como filtro, campos personalizados (custom fields) como filtro.
Esquema de Respuesta (Payload): Listado de dispositivos donde cada elemento incluye, entre otros atributos, el campo status (con valores como ACTIVATED, DEACTIVATED, PURGED, INVENTORY, TEST_READY).
Endpoint / Operación: Get Device Details
Propósito: Recuperar la información detallada de un dispositivo en particular mediante su ICCID.
Ruta confirmada: GET /api/sim/details/{iccidList}.
Parámetros de Entrada (Query/Path/Body): iccidList (parámetro de path; puede contener un ICCID para el uso canónico por SIM).
Esquema de Respuesta (Payload): Objeto con los detalles del dispositivo, incluyendo el campo status, el nombre del ratePlan (plan de tarifas) y el nombre del communicationPlan (plan de comunicación) asignados, además de atributos administrativos y de límites del dispositivo (ver secciones 4 y 5).

SECCIÓN 2: ESTATUS (Status de las líneas)
Endpoint / Operación: (Sin endpoint propio — el estado se obtiene como atributo dentro de Search Devices y Get Device Details)
Propósito: Consultar el estado actual de una línea/dispositivo. El estado no posee un endpoint dedicado; viaja como atributo dentro de las respuestas de los endpoints de consulta de dispositivos.
Parámetros de Entrada (Query/Path/Body): Los mismos parámetros de Search Devices o de Get Device Details ({iccid}).
Esquema de Respuesta (Payload): Campo status incluido en el payload de Search Devices y Get Device Details. Valores válidos mencionados: ACTIVATED, DEACTIVATED, PURGED, INVENTORY, TEST_READY, entre otros.

SECCIÓN 3: CONSUMO (Consumo de las líneas)
Endpoint / Operación: Get Device Usage
Propósito: Recuperar los detalles de uso (consumo) para un dispositivo en particular.
Ruta confirmada: GET /api/usage/simUsage.
Parámetros de Entrada (Query/Path/Body): iccid (parámetro de path).
Esquema de Respuesta (Payload): Detalles de uso del dispositivo identificado por su ICCID.
Endpoint / Operación: Get Aggregated Usage Details
Propósito: Devolver el uso agregado/agrupado para toda la empresa (cuenta), por ejemplo agrupado por país o por plan de tarifas.
Ruta confirmada: GET /api/usage/companyUsage.
Parámetros de Entrada (Query/Path/Body): Identificador de cuenta (acct) y criterios de agrupación mencionados (por país, por plan de precios).
Esquema de Respuesta (Payload): Uso agrupado de la cuenta (agregado por país o por plan de precios).

SECCIÓN 4: DETALLES ADMINISTRATIVOS DEL PLAN
Endpoint / Operación: Get Service Type Details
Propósito: Devolver los valores de los planes de comunicación y los planes de tarifas asociados al tipo de servicio.
Ruta relacionada confirmada: GET /api/sim/serviceStatus/{iccidList}.
Parámetros de Entrada (Query/Path/Body): No se especifican parámetros explícitos en el texto fuente.
Esquema de Respuesta (Payload): Valores de planes de comunicación (communicationPlan) y planes de tarifas (ratePlan).
Endpoint / Operación: Get Service Specification
Propósito: Proporcionar la especificación técnica de un servicio particular.
Parámetros de Entrada (Query/Path/Body): Identificador del servicio particular (no se detalla nombre exacto en el texto fuente).
Esquema de Respuesta (Payload): Especificación técnica del servicio.
Nota administrativa adicional: Get Device Details también devuelve los nombres del ratePlan y del communicationPlan asignados al dispositivo (mapeado como parte de la respuesta del endpoint principal de detalles del dispositivo).

SECCIÓN 5: LÍMITES (Límites de consumo)
Endpoint / Operación: (Sin endpoint propio — los límites se gestionan como atributos dentro de Get Device Details y de los detalles de la cuenta)
Propósito: Consultar o configurar los límites de consumo asociados a una cuenta o dispositivo. La documentación no define un endpoint único de "límites"; estos viajan como atributos dentro de las respuestas de los detalles de cuenta/dispositivo.
Parámetros de Entrada (Query/Path/Body): Los mismos parámetros del endpoint contenedor (por ejemplo, {iccid} en Get Device Details).
Esquema de Respuesta (Payload): Atributos relacionados con límites incluidos dentro del payload del dispositivo o cuenta:

overageLimitOverride (anulación de límite de excedente).
Test Ready Data Limit (límite de datos en estado de prueba).
Test Ready Sms Limit (límite de SMS en estado de prueba).


SECCIÓN 6: PURGA / NETWORK RESET (Status Purged)
Endpoint / Operación: PUT /api/sim/purge/
Propósito: Mover una línea a estado de purga mediante la operación pública de purga de Orion API 2.0.0.
Parámetros de Entrada (Query/Path/Body):

Body con `iccidList`, por ejemplo `{"iccidList":["8910300000000......"]}`.
Esquema de Respuesta (Payload): `200 {"status":"Ok","info":{"purged":true}}`. El flag `purged` indica si la purga fue aplicada.
Errores documentados: 400 (no iccid list), 401 (absent authorization), 403 (access denied), 500 (client not found).

Nota operativa adicional (mapeada como regla de negocio del endpoint): Un dispositivo debe encontrarse en estado PURGED como condición previa para ejecutar ciertas operaciones de retorno al inventario. El estado PURGED es uno de los valores válidos del campo status devuelto por Search Devices y Get Device Details.

Nota de escritura: el Swagger público sólo expone transiciones active, suspend y purge, además de setLimits y update name. No existe endpoint público para escribir TEST_READY, DEACTIVATED ni INVENTORY.
