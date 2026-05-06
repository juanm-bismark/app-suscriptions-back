# ADR-001 — Modular Monolith para la Subscriptions API

- **Estado**: Accepted
- **Fecha**: 2026-04-22
- **Decisores**: equipo backend
- **Relacionado**: ADR-003 (ACL + Adapter), ADR-005 (resiliencia)

## Contexto

La Subscriptions API debe centralizar lectura y operaciones sobre ~134 612 SIMs distribuidas entre tres proveedores (Kite, Tele2, Moabits). Carga esperada: 15–20 usuarios concurrentes, sin batch jobs, modo proxy en tiempo real. Equipo `< 5` ingenieros [ASSUMPTION].

Existe ya un repositorio FastAPI funcional (Identity & Access + Tenancy básico). Hay que decidir cómo organizar el código nuevo (Subscription Aggregation + Provider Integration) y la unidad de despliegue.

## Decisión

Una sola aplicación FastAPI desplegada como **modular monolith**, organizada en paquetes por bounded context:

```
app/
  identity/        # users, profiles, refresh_tokens, auth
  tenancy/         # companies, company_settings, company_provider_credentials
  subscriptions/   # canonical domain, services, routers
  providers/
    base.py        # Protocol SubscriptionProvider, errores canónicos
    registry.py    # Provider Registry
    kite/          # adapter + mappers + DTOs proveedor
    tele2/
    moabits/
  shared/          # logging, errores, middleware, http client base
```

Reglas de import:
- `subscriptions/` puede importar `providers/base.py` y `providers/registry.py`, **nunca** un adapter concreto.
- `providers/{kite,tele2,moabits}/` no se importan entre sí.
- `tenancy/` no importa de `subscriptions/`. `subscriptions/` puede consultar `tenancy/` para resolver credenciales y company scope.

Single deployable: una imagen Docker, un proceso uvicorn (o varios workers detrás del mismo container).

## Consecuencias

**Positivas**
- Cero infra adicional (no broker, no service mesh, no descubrimiento).
- Refactor a microservicios en el futuro es viable: los paquetes ya son la frontera.
- Un único pipeline de CI/CD, un único log stream, una única métrica de salud.
- Deploy < 1 min; rollback inmediato.

**Negativas / mitigaciones**
- Una caída del adapter de un proveedor puede saturar el event loop si no hay límites → **mitigación**: `asyncio.Semaphore(N)` por proveedor + circuit breaker (ADR-005).
- Cualquier cambio implica redeploy de toda la app → aceptable a 15–20 usuarios.
- El equipo tiene que respetar los límites de paquete sin fronteras técnicas duras → **mitigación**: linter `import-linter` con contratos definidos en `pyproject.toml`.

## Alternativas consideradas

1. **Microservicio por proveedor + API Gateway**
   - Pros: aislamiento de fallas, deploys independientes, escalado independiente.
   - Contras: 4+ servicios para 1 equipo de 4 personas viola el principio anti-pattern explícito (CLAUDE.md §6). Multiplica costo operativo, latencia agregada, complejidad de tracing. Sin justificación de escala (15–20 concurrentes).
   - **Rechazada**.

2. **Layered monolith actual sin reorganizar** (`models/`, `routers/`, `schemas/` planos)
   - Pros: menor refactor inicial.
   - Contras: cada feature de proveedor toca 3 carpetas; no hay frontera para evitar que la lógica del adapter Kite contamine el dominio.
   - **Rechazada**.

3. **Serverless (FaaS) por endpoint**
   - Pros: pago por uso, autoescala.
   - Contras: cold start incompatible con proxy de baja latencia; dificultad para mantener `httpx.AsyncClient` reusable y pools; dificultad para circuit breaker compartido. 134k SIMs y 15–20 usuarios no justifican el modelo de costo.
   - **Rechazada**.

## Trade-offs explícitos

| Eje | Modular monolith (elegido) | Microservicios |
|---|---|---|
| Operación | 1 servicio | N servicios + gateway + tracing distribuido |
| Aislamiento de fallas | medio (mitigado con semáforos + circuit breaker) | alto |
| Latencia | baja (sin hops) | mayor (red entre servicios) |
| Velocidad de feature | alta | media |
| Costo infra | bajo | alto |

## Cuándo revisar

- Concurrencia sostenida > 100 r/s.
- Equipo crece a > 2 squads independientes.
- Un proveedor exige aislamiento de red (VPN/IP allowlist) que pesa más que el costo de microservicio.
