# ADR-004 — Modelo de errores canónico (RFC 7807 Problem Details)

- **Estado**: Accepted
- **Fecha**: 2026-04-22
- **Decisores**: equipo backend
- **Relacionado**: ADR-003 (ACL), ADR-005 (resiliencia)

## Contexto

Tres proveedores fallan en formatos distintos: 400 con XML (Kite), JSON con `status: "error"` (Moabits), 401/403/500 con cuerpos heterogéneos (Tele2). El cliente del frontend no debe ver estos formatos. Además, la API tiene errores propios (token expirado, ICCID inválido, operación no soportada).

Sin un modelo unificado, los routers se llenarían de pattern matching sobre HTTPException con strings.

## Decisión

### 1. Jerarquía de excepciones de dominio

```python
# app/shared/errors.py
class DomainError(Exception):
    code: str            # identificador estable: "subscription.not_found"
    http_status: int     # mapeo a HTTP
    title: str           # texto humano corto
    detail: str | None   # contexto adicional opcional
    extra: dict | None   # campos específicos (provider, iccid, retry_after, …)

class SubscriptionNotFound(DomainError): code="subscription.not_found"; http_status=404
class InvalidICCID(DomainError):         code="subscription.invalid_iccid"; http_status=400
class UnsupportedOperation(DomainError): code="provider.unsupported_operation"; http_status=409
class ProviderUnavailable(DomainError):  code="provider.unavailable"; http_status=503
class ProviderRateLimited(DomainError):  code="provider.rate_limited"; http_status=429
class ProviderAuthFailed(DomainError):   code="provider.auth_failed"; http_status=502
class ProviderProtocolError(DomainError):code="provider.protocol_error"; http_status=502
class PartialResult(DomainError):        code="subscription.partial_result"; http_status=207
class CredentialsMissing(DomainError):   code="tenant.credentials_missing"; http_status=412
class ForbiddenOperation(DomainError):   code="auth.forbidden"; http_status=403
```

### 2. Cada adapter traduce errores externos a esta jerarquía

```python
# app/providers/kite/adapter.py (ejemplo)
try:
    response = await self._client.post(...)
    response.raise_for_status()
except httpx.TimeoutException as e:
    raise ProviderUnavailable(detail="Kite timeout", extra={"provider": "KITE"}) from e
except httpx.HTTPStatusError as e:
    if e.response.status_code == 401:
        raise ProviderAuthFailed(extra={"provider": "KITE"}) from e
    if e.response.status_code == 429:
        raise ProviderRateLimited(extra={"provider": "KITE", "retry_after": e.response.headers.get("Retry-After")}) from e
    raise ProviderProtocolError(detail=f"Kite returned {e.response.status_code}", extra={"provider": "KITE"}) from e
```

### 3. Handler global serializa a RFC 7807

```python
# app/main.py
@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "type": f"https://api.example.com/errors/{exc.code}",
            "title": exc.title,
            "status": exc.http_status,
            "code": exc.code,
            "detail": exc.detail,
            "instance": request.headers.get("X-Request-ID"),
            **(exc.extra or {}),
        },
        headers={"Content-Type": "application/problem+json"},
    )
```

### 4. Listados con resultados parciales

Cuando una página global respaldada por `sim_routing_map` puede devolver datos útiles pero alguna llamada de proveedor falla, **no** se levanta excepción. El service devuelve un `Page` con:

```json
{
  "items": [...],
  "next_cursor": "...",
  "partial": true,
  "failed_providers": [
    {"provider": "TELE2", "code": "provider.unavailable", "title": "Provider Tele2 is unreachable"}
  ]
}
```

HTTP `200` (no 207, porque el shape devuelto sigue siendo un listado válido). Sólo se emite error si la operación completa no puede devolver datos servibles.

## Consecuencias

**Positivas**
- El cliente recibe siempre el mismo formato de error con `code` estable que puede branchear.
- Los routers no contienen pattern matching sobre proveedores.
- Logging y métricas se etiquetan por `code` y `provider` → dashboards triviales.
- Si mañana se agrega Provider #4, los `code` no cambian.

**Negativas / mitigaciones**
- Más boilerplate de excepciones → **mitigación**: factory `dataclass`-based, prácticamente declarativa.
- Riesgo de filtrar `detail` con info sensible del proveedor → **mitigación**: revisión en code review + scrubber opcional en producción que recorta `detail` para errores 5xx.

## Alternativas consideradas

1. **Levantar `HTTPException(status_code, detail=str)` directamente**
   - Pros: nativo de FastAPI.
   - Contras: el cliente no tiene un `code` estable; el formato lo define cada quien; difícil de reusar.
   - **Rechazada**.

2. **Errores específicos por proveedor expuestos al cliente** (ej. `KiteError`)
   - Pros: granularidad máxima.
   - Contras: filtra vocabulario externo; el frontend tendría que conocer 3 vocabularios.
   - **Rechazada**.

## Trade-offs explícitos

| Eje | RFC 7807 + jerarquía (elegido) | HTTPException ad-hoc |
|---|---|---|
| Consistencia para el cliente | alta | baja |
| Branching en frontend | sobre `code` estable | sobre strings frágiles |
| Esfuerzo inicial | medio | bajo |
| Esfuerzo en N+1 proveedor | bajo | crece linealmente |

## Cuándo revisar

- Si la app expone GraphQL en algún momento, los errores se moverán a su propio modelo (`errors[]` array).
