# Platform security and durable history release gate

This release keeps the shared `core-api`/control Worker control plane, but
platform runners and Workers can run with separate MySQL users, Redis ACL
users, and encryption keys. `DPMS_PLATFORM_SECURITY_MODE=strict` is required
for production platform lanes.

> **Database routing gate (not yet delivered).** `isolated` MySQL mode currently
> provisions separate schemas and runtime grants, but the shared `core-api`
> still reads and writes its single `DATABASE_URL`; there is no platform-aware
> API router or replication path to copy API-created tasks/accounts/lotteries
> into the four platform databases (or to surface platform discovery results
> back to the shared API database). Therefore do **not** enable
> `DPMS_MYSQL_PLATFORM_DATABASE_MODE=isolated` in production yet: the platform
> Workers would not see API-created work and the dashboard would not see
> platform-local results. Do not deploy this release to production with either
> mode until the database router and its migration/continuity checks are
> delivered as a separate release. `shared` mode remains a local/rolling-
> upgrade compatibility mode, not a production isolation boundary. Redis ACL
> and encryption-key isolation remain valid independently of this gate.

## Provisioning order (deferred for production)

The following is the required order for the follow-up release that closes the
database-routing gate. It must not be executed against production yet.

1. Stop the old Core and Worker deployment. Do not run old and new protocols
   against the same Redis streams during the cutover.
2. Set all four platform database names/users/passwords, eight scoped Redis
   passwords, and four `ENCRYPTION_KEY_<PLATFORM>` values. Use distinct values
   from the shared control credentials.
3. Set `DPMS_MYSQL_PLATFORM_DATABASE_MODE=isolated` and
   `DPMS_PLATFORM_SECURITY_MODE=strict`.
4. Run the MySQL provisioning container. It creates the four databases and
   grants the migration user full DDL access plus each platform runtime user
   only DML/EXECUTE on its own database.
5. Run `core-migrate` for the shared control database and each of
   `core-migrate-bilibili`, `core-migrate-weibo`,
   `core-migrate-xiaohongshu`, and `core-migrate-douyin` from the migration
   profile. Migration `0031` records the MySQL 8 installation baseline; the
   frozen `0011` checksum is not changed.
6. Run the schema verifier for every database and inspect Redis ACL preflight
   for every scoped Core/Worker identity.
7. Start the new `core-api`, platform runners, control Worker, and platform
   Workers. Confirm each lane reports its own database/Redis identity before
   enabling real-run.

The follow-up release must also prove API task/account/lottery routing and
platform result aggregation before step 7. Database grants alone are not
evidence of end-to-end data-plane isolation.

## Notification delivery

Migration `0030` creates `notification_delivery_attempts`. A sender claims a
delivery before calling the provider and marks it `sent` only after the
provider response and notify-log update succeed. A stale `sending` claim is
changed to `uncertain`, not retried automatically. This preserves at-least-once
transport while avoiding an unbounded duplicate side effect after a crash.

## Outbox archive

Archiving is disabled by default. Enable it only after an operator has observed
the global Redis continuity epoch (without a stream argument) and recorded a
contiguous per-stream watermark with
`set_outbox_archive_watermark(stream_key, safe_outbox_id, continuity_epoch)`.
The background pass copies only `sent` rows older than the retention window
below that watermark. A pending/failed/unsent row at or below the boundary,
or missing/mismatched global-epoch evidence, produces a zero-row pass. The
lane epoch is not used as the archive fence because normal ACK/XDEL drains a
platform lane and rotates that lane's recovery epoch. Rows are marked with
`archived_at` and remain available until the separate, longer-retention purge
procedure is explicitly run.

## Legacy admin-token clients

The new API accepts admin credentials in either the standard
`Authorization: Bearer <token>` header or the existing `X-Admin-Token` header.
Before cutover, inventory EventSource clients, image URLs, scripts, and
bookmarks that still send `?admin_token=`; they will receive `401` after the
header-only gate is enabled.
