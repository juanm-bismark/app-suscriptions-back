# Manual Migration Order

Run `000_init.sql` first, then apply these files in numeric order:

1. `001_sim_routing_map.sql` — SIM routing only.
2. `002_company_provider_credentials.sql` — encrypted provider credentials.
3. `003_audit_log.sql` — generic immutable audit log.
4. `004_idempotency_keys.sql` — company-scoped idempotency keys.
5. `005_lifecycle_change_audit.sql` — SIM lifecycle write audit.
6. `006_moabits_source_companies.sql` — Moabits discovered company cache.
7. `007_company_provider_mappings.sql` — company-scoped provider company mapping.

These are clean initial migrations.
