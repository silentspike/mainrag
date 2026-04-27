# Security policy

> **Status: Pre-Alpha (`v0.1.0-alpha.1`).** This document reflects the
> security model of MainRag as it exists today. It is honest about
> what is enforced and what is deferred.

## 1. Threat model

MainRag is a **single-tenant developer preview**. The threat model
explicitly assumes:

- The deploying party owns the infrastructure end to end (Postgres,
  Qdrant, the embedder/reranker GPU host, the API host).
- Tenants are non-malicious internal users (engineers, agents acting
  on their behalf). Hardening against a malicious authenticated
  tenant — exhaustive RLS coverage, audit-log shipping, per-tenant
  quotas — is scoped for v0.2 (multi-tenant beta).
- Network ingress is a private VPN / company LAN. Public-internet
  exposure is **not** a supported deployment.
- Only signed-and-trusted code is indexed. The system does not
  sandbox tree-sitter parsing or reject malicious inputs designed to
  abuse the parser — running it against attacker-controlled corpora
  is out of scope.

If your threat model differs, do not deploy `v0.1.0-alpha`. Track v0.2
in the milestones.

## 2. Tenant isolation

Per-tenant access is enforced **inside Postgres** through Row-Level
Security and **inside Qdrant** through payload filters set on every
search request. The two paths are kept consistent by funnelling all
requests through the API service.

- **PostgreSQL RLS — enabled tables:** `sources`, `files`, `chunks`.
  Policies use `current_setting('request.user_id')`, which is set per
  request in the API auth middleware before any query runs.
- **PostgreSQL RLS — known gaps:** `symbols`, `call_graph_edges`,
  `symbol_cards`, `symbol_annotations`, `negative_evidence` are
  **not** RLS-gated in alpha. They derive from RLS-gated parents
  (`chunks`/`files`) but a direct query that bypasses the parent join
  is not blocked at the row level. v0.2 will add policies; until
  then, the API layer is the only enforcement and the deployment
  must trust its callers.
- **Qdrant tenant filter:** every search emits a
  `must: [{ key: "user_id", match: { value: <jwt-uid> } }]` filter.
  The collection is shared; isolation is filter-driven.
- **`HealthPool` exception:** the health-check / cron pool runs
  with a privileged role that bypasses RLS by design. This pool is
  used only for `SELECT 1`-style probes and the metrics endpoint.

The transactional outbox + the hardcoded `DEFAULT_USER_ID` constant
are tracked as the v0.2 hardening blocker:
[#10](https://github.com/silentspike/mainrag/issues/10).

## 3. Secrets handling

- All secrets live in a single env-only file. Production deployments
  are expected to use `/etc/mainrag/mainrag.env` (mode `0600`,
  ownership `root:mainrag`) loaded by the systemd unit through
  `EnvironmentFile=`.
- The repo ships `mainrag.env.example` as a template with
  `<SET_*>` and `<GENERATE_VIA: ...>` placeholders. The real
  `mainrag.env` is `.gitignore`d.
- Repo history was rewritten with `git-filter-repo` before the
  public flip. `gitleaks` and `trufflehog` both report **0 findings**
  on the rewritten history.
- Secrets that were ever committed pre-rewrite were rotated *before*
  the rewrite, so no value found via a third-party fork of an old
  blob is currently valid against any live system.

## 4. Qdrant authentication

Server-side `api_key` authentication is **enabled** in
`docker-compose.yml`:

```yaml
qdrant:
  environment:
    - QDRANT__SERVICE__API_KEY=${QDRANT_API_KEY:?...}
```

Anonymous requests to `/collections` return **HTTP 401**. The
client-side `QDRANT_API_KEY` in `mainrag.env` must match the
server-side value or the API will fail health-checks at startup.

Earlier internal documentation occasionally implied that the
default-compose deployment ran without auth. That referred to a
pre-public state and is no longer true; this section is the single
source of truth.

## 5. RLS guarantees per table

| Table                | RLS-enabled? | Policy source                | Notes |
| -------------------- | :----------: | ---------------------------- | ----- |
| `sources`            | Yes          | `schema_security.sql`        | User-isolation + admin-bypass policies |
| `files`              | Yes          | `schema_security.sql`        | User-isolation + admin-bypass policies |
| `chunks`             | Yes          | `schema_security.sql`        | User-isolation + admin-bypass policies |
| `symbols`            | No           | -                            | v0.2 hardening (Issue #10 follow-up) |
| `call_graph_edges`   | No           | -                            | v0.2 hardening |
| `symbol_cards`       | No           | -                            | v0.2 hardening |
| `symbol_annotations` | No           | -                            | v0.2 hardening |
| `negative_evidence`  | No           | -                            | v0.2 hardening |
| `users`, `api_keys`  | No           | API-layer only               | Admin-only HTTP routes; never tenant-reachable |

`symbol_cards` and friends are reachable only through the API
intelligence routes, which always join through an RLS-gated parent
(`chunks` → `files`). A direct SQL connection by a tenant could
bypass the join — this is the documented v0.2 gap.

## 6. Known alpha limits

- **Outbox / drift between Postgres and Qdrant.**
  ([#10](https://github.com/silentspike/mainrag/issues/10)) Indexer
  writes Postgres rows and Qdrant points in separate transactions.
  A crash mid-run can leave orphans. Admin
  `/api/v1/admin/backfill/orphaned` mitigates after the fact; the
  outbox-driven design is v0.2 work.
- **`DEFAULT_USER_ID` constant.** Watcher / admin ingestion paths
  use a hardcoded UUID. RLS treats this UUID as just another tenant,
  so audit trails are misleading on those paths.
- **No per-tenant rate limits.** The API rate-limiter (`tower-limit`)
  is global, not per `user_id`. A noisy tenant degrades all tenants.
- **No audit-log shipping.** Postgres logs and `journalctl` are the
  only audit surface. There is no SIEM-friendly export.
- **CodeQL on Rust** needs GitHub Advanced Security on private repos;
  the workflow is `continue-on-error` so the signal surfaces but
  does not gate merges.

## 7. Reporting a vulnerability

Please report security issues through **GitHub Security Advisories**:

> https://github.com/silentspike/mainrag/security/advisories/new

This is the only supported channel. Do not file public issues for
vulnerability reports and do not post to social media first.

**Triage SLA (Pre-Alpha):**

- Acknowledgement: within 7 days of the advisory submission.
- Initial assessment + severity rating: within 14 days.
- Fix or documented mitigation: within 30 days for critical / high,
  best-effort for medium / low.

The SLA improves with v0.2 / multi-tenant beta. As long as MainRag
is in `0.1.0-alpha`, the response cadence is realistic for a single
maintainer, not a vendor pager rotation.
