# Manual Migration Order

Run `init.sql` first, then apply these files in numeric order:

1. `001_sim_routing_map.sql` — SIM routing only.
2. `002_company_provider_credentials.sql` — encrypted provider credentials.
3. `003_audit_log.sql` — generic immutable audit log.
4. `004_idempotency_keys.sql` — company-scoped idempotency keys.
5. `005_lifecycle_change_audit.sql` — SIM lifecycle write audit.
6. `006_provider_source_configs.sql` — global non-secret provider source config.

These are clean initial migrations.
