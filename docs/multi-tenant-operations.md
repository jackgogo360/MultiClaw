# Multi-Tenant Operations

This document covers the current standalone deployment and operational contract for MultiClaw's multi-tenant release gate. It intentionally avoids DSNs, tokens, email addresses, and real filesystem paths.

## Deployment Inputs

- Configure exactly one `database.driver` and one `database.url`.
- `database.driver` must match the `database.url` scheme:
  - `sqlite` uses the SQLite async SQLAlchemy URL form
  - `mysql` uses the MySQL async SQLAlchemy URL form
- Supported backends are SQLite and MySQL. Do not configure both for one deployment and do not keep a second standby backend in the same process config.

## Database Release Procedure

Before starting or reintroducing API traffic for a deployment:

1. Run `multiclaw db upgrade`.
2. Run `multiclaw db check`.
3. Start the API only after both commands succeed.

The API does not run migrations automatically. `/api/health/ready` fails closed on schema drift; it is not a migration hook.

Before any future forward-only upgrade, take a backup of a production-like database and verify that restore works before changing live traffic.

## JWT Signing Key

- Configure the JWT signing key from exactly one source:
  - environment variable `MULTICLAW_AUTH_JWT_SIGNING_KEY`, or
  - config key `auth.jwt_signing_key_file`
- Do not configure both sources and do not leave both unset.
- The loaded key material must contain at least 32 bytes.
- If you use `auth.jwt_signing_key_file`, keep the file non-world-readable, operator-managed, and outside any user-controlled workspace content.

## Secret Keyring

- Configure the deployment keyring from exactly one source:
  - environment variable `MULTICLAW_SECRETS_KEYRING_B64`, or
  - config key `secrets.keyring_file`
- Do not configure both sources and do not leave both unset.
- The keyring must keep one active version and every older version that is still referenced by database rows.
- Record the active key version and the retained old versions in your deployment change record. Do not store raw key payloads, tokens, or filesystem paths in that record.
- If you use `secrets.keyring_file`, apply the same operator-managed and restrictive-permission handling as the JWT file source.

## Health Endpoints And Traffic Gates

- `/api/health/live` only proves that the process is alive.
- `/api/health/ready` is the traffic gate. It returns ready only when all current invariants hold, including:
  - database connectivity
  - supported backend version
  - schema revision at the current Alembic head
  - schema integrity and foreign-key checks
  - workspace root permissions
  - active default workspace integrity
  - keyring load plus referenced-version validation
- For MySQL, readiness also requires InnoDB tables, `utf8mb4`, a UTC-compatible session time zone, and `READ COMMITTED`.
- If readiness fails, remove the instance from traffic and investigate. Do not use readiness failure as a reason to auto-run migrations in place.

## Purge Worker

- In the current standalone deployment, the API lifespan starts the purge worker only when startup readiness is healthy.
- The worker runs as a cancellable batch-polling loop. Account deletion with retention `0` still completes asynchronously through that worker path.
- Monitor the low-cardinality counter `multiclaw_purge_retry_total`.
- When a purge stalls or retries, inspect the tenant's deletion job state, especially `status`, `worker_id`, `lease_expires_at`, `heartbeat_at`, `attempt_count`, and `last_error`, and also confirm whether blocking activity is still present.
- Do not manually cascade or delete tenant rows out of order. The scoped purge path enforces its own deletion order and fencing rules.
- Backups, traces, and incident notes for purge operations must not include email addresses, filesystem paths, or secret material.

## Key Rotation

1. Add the new key version to the keyring.
2. Mark that new version as active.
3. Keep all older versions that are still referenced by database rows.
4. Because there is currently no productized rotation runner or CLI, handle rotation only through an operator-authored, code-reviewed one-off script or internal maintenance service running in a controlled maintenance environment.
5. In that maintenance path, explicitly construct the current rotation service and invoke `SecretRotationService.rotate_batch()` in batches.
6. If a batch fails, stop the rotation attempt and keep all older key versions.
7. Monitor the returned `rotated`, `skipped`, and `failed` counts for each batch.
8. Remove an old key version only after the database no longer references it.

## Backend-Specific Notes

- SQLite:
  - place the database on durable storage
  - use backup methods that preserve file consistency
  - treat file copies taken during active writes as unsafe unless your platform snapshot method guarantees consistency
- MySQL:
  - run MySQL `8.0.36` or newer on major version `8`, including `8.4.x`
  - use InnoDB
  - keep the database and tables on `utf8mb4`
  - keep sessions UTC-compatible
  - keep transaction isolation at `READ COMMITTED`

## V1 Non-Goals

The following are explicitly out of scope for v1:

- legacy data migration
- dual-write
- workspace switcher
- cluster deployment
- KMS or Vault integration
- superadmin flows
- same-run parallel tools

The product is not released yet, so do not plan or execute historical tenant backfill.
