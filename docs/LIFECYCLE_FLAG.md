LIFECYCLE_WRITES_ENABLED

Purpose
- Controls whether provider write operations (set status and the canonical `purge`
  control operation)
  are allowed by the runtime. This protects production systems from accidental
  writes during development or while running integration tests.

Default
- `false` in the shipped `.env.example`.

Behavior
- When `false`, adapters will raise `UnsupportedOperation` for write-paths.
- When `true`, adapters that implement write operations will perform provider
  calls. Use only against sandbox/test provider endpoints until validated.
- `POST /v1/sims/{iccid}/purge` is canonical. Adapters translate it to the
  provider primitive: Kite `networkReset`, Tele2 `status=PURGED`, Moabits
  `PUT /api/sim/purge/` with `{ "iccidList": [...] }`.

How to enable locally
- Copy `.env.example` to `.env` and set:

```bash
LIFECYCLE_WRITES_ENABLED=true
```

Operational guidance
- Enable the flag only for short-lived smoke tests against sandbox endpoints.
- Use the provider's sandbox credentials in your `.env` or secret manager.
- Ensure you have monitoring/alerts for provider errors and request-volume.
