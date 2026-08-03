#!/bin/sh
set -eu

# The upstream image normally drops privileges in docker-entrypoint.sh. DPMS
# replaces that entrypoint, so reproduce the safe /data ownership hand-off and
# then permanently run both this supervisor and redis-server as uid 999.
if [ "$(id -u)" = "0" ]; then
  find /data -xdev ! -user redis -exec chown -h redis:redis '{}' +
  exec /usr/bin/setpriv \
    --reuid redis \
    --regid redis \
    --clear-groups \
    "$0" "$@"
fi
if [ "$(id -u)" = "0" ]; then
  echo "redis privilege drop failed" >&2
  exit 1
fi

: "${REDIS_CORE_PASSWORD:?REDIS_CORE_PASSWORD is required}"
: "${REDIS_WORKER_PASSWORD:?REDIS_WORKER_PASSWORD is required}"
: "${REDIS_HEALTH_PASSWORD:?REDIS_HEALTH_PASSWORD is required}"
: "${REDIS_GROUP_ADMIN_PASSWORD:?REDIS_GROUP_ADMIN_PASSWORD is required}"

# Isolated platform lanes use identities that cannot read another platform's
# stream keys.  Defaults keep local Compose development frictionless; strict
# production mode below rejects every built-in value.
REDIS_CORE_BILIBILI_PASSWORD="${REDIS_CORE_BILIBILI_PASSWORD:-dpms-core-bilibili-local-only-change-me-2026}"
REDIS_WORKER_BILIBILI_PASSWORD="${REDIS_WORKER_BILIBILI_PASSWORD:-dpms-worker-bilibili-local-only-change-me-2026}"
REDIS_CORE_WEIBO_PASSWORD="${REDIS_CORE_WEIBO_PASSWORD:-dpms-core-weibo-local-only-change-me-2026}"
REDIS_WORKER_WEIBO_PASSWORD="${REDIS_WORKER_WEIBO_PASSWORD:-dpms-worker-weibo-local-only-change-me-2026}"
REDIS_CORE_XIAOHONGSHU_PASSWORD="${REDIS_CORE_XIAOHONGSHU_PASSWORD:-dpms-core-xiaohongshu-local-only-change-me-2026}"
REDIS_WORKER_XIAOHONGSHU_PASSWORD="${REDIS_WORKER_XIAOHONGSHU_PASSWORD:-dpms-worker-xiaohongshu-local-only-change-me-2026}"
REDIS_CORE_DOUYIN_PASSWORD="${REDIS_CORE_DOUYIN_PASSWORD:-dpms-core-douyin-local-only-change-me-2026}"
REDIS_WORKER_DOUYIN_PASSWORD="${REDIS_WORKER_DOUYIN_PASSWORD:-dpms-worker-douyin-local-only-change-me-2026}"
REDIS_CORE_BILIBILI_USERNAME="${REDIS_CORE_BILIBILI_USERNAME:-core-bilibili}"
REDIS_WORKER_BILIBILI_USERNAME="${REDIS_WORKER_BILIBILI_USERNAME:-worker-bilibili}"
REDIS_CORE_WEIBO_USERNAME="${REDIS_CORE_WEIBO_USERNAME:-core-weibo}"
REDIS_WORKER_WEIBO_USERNAME="${REDIS_WORKER_WEIBO_USERNAME:-worker-weibo}"
REDIS_CORE_XIAOHONGSHU_USERNAME="${REDIS_CORE_XIAOHONGSHU_USERNAME:-core-xiaohongshu}"
REDIS_WORKER_XIAOHONGSHU_USERNAME="${REDIS_WORKER_XIAOHONGSHU_USERNAME:-worker-xiaohongshu}"
REDIS_CORE_DOUYIN_USERNAME="${REDIS_CORE_DOUYIN_USERNAME:-core-douyin}"
REDIS_WORKER_DOUYIN_USERNAME="${REDIS_WORKER_DOUYIN_USERNAME:-worker-douyin}"

deployment_mode="$(
  printf '%s' "${DEPLOYMENT_MODE:-dev}" | tr '[:upper:]' '[:lower:]'
)"

validate_production_password() {
  password_name="$1"
  password_value="$2"
  development_value="$3"
  if [ "$deployment_mode" != "production" ]; then
    return
  fi
  if [ "${#password_value}" -lt 24 ]; then
    echo "${password_name} must contain at least 24 characters in production" >&2
    exit 1
  fi
  if [ "$password_value" = "$development_value" ]; then
    echo "${password_name} development value is forbidden in production" >&2
    exit 1
  fi
}

validate_production_password \
  REDIS_CORE_PASSWORD \
  "$REDIS_CORE_PASSWORD" \
  "dpms-core-local-only-change-me-2026"
validate_production_password \
  REDIS_WORKER_PASSWORD \
  "$REDIS_WORKER_PASSWORD" \
  "dpms-worker-local-only-change-me-2026"
validate_production_password \
  REDIS_HEALTH_PASSWORD \
  "$REDIS_HEALTH_PASSWORD" \
  "dpms-health-local-only-change-me-2026"
validate_production_password \
  REDIS_GROUP_ADMIN_PASSWORD \
  "$REDIS_GROUP_ADMIN_PASSWORD" \
  "dpms-group-admin-local-only-change-me-2026"
validate_production_password REDIS_CORE_BILIBILI_PASSWORD "$REDIS_CORE_BILIBILI_PASSWORD" "dpms-core-bilibili-local-only-change-me-2026"
validate_production_password REDIS_WORKER_BILIBILI_PASSWORD "$REDIS_WORKER_BILIBILI_PASSWORD" "dpms-worker-bilibili-local-only-change-me-2026"
validate_production_password REDIS_CORE_WEIBO_PASSWORD "$REDIS_CORE_WEIBO_PASSWORD" "dpms-core-weibo-local-only-change-me-2026"
validate_production_password REDIS_WORKER_WEIBO_PASSWORD "$REDIS_WORKER_WEIBO_PASSWORD" "dpms-worker-weibo-local-only-change-me-2026"
validate_production_password REDIS_CORE_XIAOHONGSHU_PASSWORD "$REDIS_CORE_XIAOHONGSHU_PASSWORD" "dpms-core-xiaohongshu-local-only-change-me-2026"
validate_production_password REDIS_WORKER_XIAOHONGSHU_PASSWORD "$REDIS_WORKER_XIAOHONGSHU_PASSWORD" "dpms-worker-xiaohongshu-local-only-change-me-2026"
validate_production_password REDIS_CORE_DOUYIN_PASSWORD "$REDIS_CORE_DOUYIN_PASSWORD" "dpms-core-douyin-local-only-change-me-2026"
validate_production_password REDIS_WORKER_DOUYIN_PASSWORD "$REDIS_WORKER_DOUYIN_PASSWORD" "dpms-worker-douyin-local-only-change-me-2026"

validate_acl_username() {
  username_name="$1"
  username_value="$2"
  if ! printf '%s' "$username_value" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'; then
    echo "${username_name} is invalid" >&2
    exit 1
  fi
  case "$username_value" in
    core|worker|health|group-admin)
      echo "${username_name} must not reuse a shared Redis ACL identity" >&2
      exit 1
      ;;
  esac
}

seen_platform_usernames="|"
for username_name in \
  REDIS_CORE_BILIBILI_USERNAME REDIS_WORKER_BILIBILI_USERNAME \
  REDIS_CORE_WEIBO_USERNAME REDIS_WORKER_WEIBO_USERNAME \
  REDIS_CORE_XIAOHONGSHU_USERNAME REDIS_WORKER_XIAOHONGSHU_USERNAME \
  REDIS_CORE_DOUYIN_USERNAME REDIS_WORKER_DOUYIN_USERNAME; do
  eval "username_value=\${${username_name}}"
  validate_acl_username "$username_name" "$username_value"
  case "$seen_platform_usernames" in
    *"|${username_value}|"*)
      echo "Redis platform ACL usernames must be mutually distinct" >&2
      exit 1
      ;;
  esac
  seen_platform_usernames="${seen_platform_usernames}${username_value}|"
done

if [ "$REDIS_CORE_PASSWORD" = "$REDIS_WORKER_PASSWORD" ] \
  || [ "$REDIS_CORE_PASSWORD" = "$REDIS_HEALTH_PASSWORD" ] \
  || [ "$REDIS_CORE_PASSWORD" = "$REDIS_GROUP_ADMIN_PASSWORD" ] \
  || [ "$REDIS_WORKER_PASSWORD" = "$REDIS_HEALTH_PASSWORD" ] \
  || [ "$REDIS_WORKER_PASSWORD" = "$REDIS_GROUP_ADMIN_PASSWORD" ] \
  || [ "$REDIS_HEALTH_PASSWORD" = "$REDIS_GROUP_ADMIN_PASSWORD" ]; then
  echo "Redis ACL passwords must be mutually distinct" >&2
  exit 1
fi

for platform in bilibili weibo xiaohongshu douyin; do
  suffix="$(printf '%s' "$platform" | tr '[:lower:]' '[:upper:]')"
  eval "platform_core_password=\${REDIS_CORE_${suffix}_PASSWORD}"
  eval "platform_worker_password=\${REDIS_WORKER_${suffix}_PASSWORD}"
  eval "platform_core_username=\${REDIS_CORE_${suffix}_USERNAME}"
  eval "platform_worker_username=\${REDIS_WORKER_${suffix}_USERNAME}"
  if [ "$platform_core_password" = "$platform_worker_password" ] \
    || [ "$platform_core_password" = "$REDIS_CORE_PASSWORD" ] \
    || [ "$platform_worker_password" = "$REDIS_WORKER_PASSWORD" ]; then
    echo "Redis platform ACL passwords must be mutually distinct" >&2
    exit 1
  fi
  if [ "$platform_core_username" = "$platform_worker_username" ]; then
    echo "Redis platform ACL usernames must be mutually distinct" >&2
    exit 1
  fi
done

topology_file="${REDIS_CONSUMER_GROUP_TOPOLOGY_FILE:-/usr/local/share/dpms/consumer-groups.tsv}"
if [ ! -r "$topology_file" ]; then
  echo "fixed Redis consumer-group topology is unavailable" >&2
  exit 1
fi

umask 077

password_hash() {
  printf '%s' "$1" | sha256sum | awk '{print $1}'
}

core_hash="$(password_hash "$REDIS_CORE_PASSWORD")"
worker_hash="$(password_hash "$REDIS_WORKER_PASSWORD")"
health_hash="$(password_hash "$REDIS_HEALTH_PASSWORD")"
group_admin_hash="$(password_hash "$REDIS_GROUP_ADMIN_PASSWORD")"
core_bilibili_hash="$(password_hash "$REDIS_CORE_BILIBILI_PASSWORD")"
worker_bilibili_hash="$(password_hash "$REDIS_WORKER_BILIBILI_PASSWORD")"
core_weibo_hash="$(password_hash "$REDIS_CORE_WEIBO_PASSWORD")"
worker_weibo_hash="$(password_hash "$REDIS_WORKER_WEIBO_PASSWORD")"
core_xiaohongshu_hash="$(password_hash "$REDIS_CORE_XIAOHONGSHU_PASSWORD")"
worker_xiaohongshu_hash="$(password_hash "$REDIS_WORKER_XIAOHONGSHU_PASSWORD")"
core_douyin_hash="$(password_hash "$REDIS_CORE_DOUYIN_PASSWORD")"
worker_douyin_hash="$(password_hash "$REDIS_WORKER_DOUYIN_PASSWORD")"
bootstrap_password="$(
  od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
)"
if [ "${#bootstrap_password}" -ne 64 ]; then
  echo "failed to create one-time Redis bootstrap credential" >&2
  exit 1
fi
bootstrap_hash="$(password_hash "$bootstrap_password")"

# Keep the transient wide AOF-replay ACL out of the persistent data volume.
# Compose provides /tmp as an in-memory filesystem and every start recreates
# both phases from the configured secret hashes.
acl_tmp="/tmp/users.acl.tmp"
acl_file="/tmp/users.acl"

# Runtime selectors cover only DPMS keys. The smaller governed selector is
# used by the one-time bootstrap and the separately held retirement identity.
runtime_keys="~lottery_tasks ~lottery_tasks:* ~lottery_repair_tasks:v1:* ~adapter_probe_requests ~adapter_probe_requests:* ~account_calibration_requests ~account_calibration_requests:* ~discovery_scan_requests:v1:* ~discovery_scan_result:v1:* ~xiaohongshu_target_pursuit_requests:v1 ~xiaohongshu_target_pursuit_result:v1:* ~notify_events ~failed_task_messages ~login_requests ~legacy_task_fanout:* ~account_calibration_legacy_fanout:* ~account_calibration_requeue:* ~recovery_count:* ~dpms:task-stream:* ~daily_limit:* ~risk_window:* ~update_signal"
governed_stream_keys="~lottery_tasks ~lottery_tasks:* ~lottery_repair_tasks:v1:* ~adapter_probe_requests ~adapter_probe_requests:* ~account_calibration_requests ~account_calibration_requests:* ~discovery_scan_requests:v1:* ~xiaohongshu_target_pursuit_requests:v1 ~notify_events ~login_requests"

# Redis replays AOF commands with its synthetic client attached to the default
# ACL user. Keep that user fully capable only while protected mode prevents
# non-loopback clients from connecting; otherwise persisted MULTI/EXEC blocks
# are rejected before the final named-user ACL can be activated.
#
# The supervisor uses only the unguessable named bootstrap identity during this
# phase. It can wait for loading, create governed groups, and atomically load
# the final ACL file; it cannot read payloads, mutate entries, destroy groups,
# or touch other keys.
cat >"$acl_tmp" <<EOF
user default on nopass ${runtime_keys} &* +@all
user bootstrap on #${bootstrap_hash} resetkeys resetchannels -@all +ping +info +acl|whoami +acl|load (+xinfo|groups +xgroup|create ${governed_stream_keys})
EOF
mv "$acl_tmp" "$acl_file"

redis-server \
  --dir /data \
  --appendonly yes \
  --appendfsync everysec \
  --protected-mode yes \
  --aclfile "$acl_file" &
redis_pid="$!"

stop_redis() {
  status="$1"
  trap - TERM INT
  if kill -0 "$redis_pid" 2>/dev/null; then
    kill -TERM "$redis_pid" 2>/dev/null || true
  fi
  set +e
  wait "$redis_pid"
  set -e
  exit "$status"
}
trap 'stop_redis 143' TERM
trap 'stop_redis 130' INT

bootstrap_abort() {
  echo "$1" >&2
  stop_redis 1
}

bootstrap_cli() {
  REDISCLI_AUTH="$bootstrap_password" redis-cli \
    --no-auth-warning \
    --raw \
    --user bootstrap \
    -h 127.0.0.1 \
    -p 6379 \
    "$@"
}

attempt=0
bootstrap_ready=false
while [ "$attempt" -lt 3000 ]; do
  bootstrap_identity="$(bootstrap_cli ACL WHOAMI 2>/dev/null || true)"
  persistence_info="$(bootstrap_cli INFO persistence 2>/dev/null || true)"
  if [ "$bootstrap_identity" = "bootstrap" ] \
    && bootstrap_cli PING >/dev/null 2>&1 \
    && printf '%s\n' "$persistence_info" | grep -Eq '^loading:0\r?$'; then
    bootstrap_ready=true
    break
  fi
  if ! kill -0 "$redis_pid" 2>/dev/null; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 0.1
done
if [ "$bootstrap_ready" != "true" ]; then
  bootstrap_abort "Redis dataset did not finish loading"
fi

tab="$(printf '\t')"
topology_count=0
while IFS="$tab" read -r stream_key group_name extra; do
  case "$stream_key" in
    ""|\#*) continue ;;
  esac
  if [ -z "${group_name:-}" ] || [ -n "${extra:-}" ]; then
    bootstrap_abort "Redis consumer-group topology row is invalid"
  fi
  for topology_name in "$stream_key" "$group_name"; do
    if ! printf '%s\n' "$topology_name" \
      | grep -Eq '^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$'; then
      bootstrap_abort "Redis consumer-group topology name is invalid"
    fi
  done
  create_result="$(
    bootstrap_cli \
      XGROUP CREATE "$stream_key" "$group_name" 0 MKSTREAM \
      2>&1
  )" || true
  case "$create_result" in
    OK|*BUSYGROUP*) ;;
    *) bootstrap_abort "fixed Redis consumer-group creation failed" ;;
  esac
  topology_count=$((topology_count + 1))
done <"$topology_file"
if [ "$topology_count" -le 0 ]; then
  bootstrap_abort "Redis consumer-group topology is empty"
fi

# Verify every exact pair before making runtime identities available. Extra
# historical groups remain untouched for the explicit retirement workflow.
while IFS="$tab" read -r stream_key group_name extra; do
  case "$stream_key" in
    ""|\#*) continue ;;
  esac
  groups="$(
    bootstrap_cli XINFO GROUPS "$stream_key" 2>&1
  )" || bootstrap_abort "fixed Redis consumer-group verification failed"
  if ! printf '%s\n' "$groups" | awk -v expected="$group_name" '
    previous == "name" && $0 == expected { found = 1 }
    { previous = $0 }
    END { exit(found ? 0 : 1) }
  '; then
    bootstrap_abort "fixed Redis consumer group is missing"
  fi
done <"$topology_file"

# Build the exact key selectors for one platform identity. Shared control
# streams are deliberately absent from this list; a platform Worker may only
# append to notify_events and a platform Core may only own its own lanes.
platform_data_keys() {
  platform="$1"
  keys="$(printf '%s' \
    "~lottery_tasks:${platform} ~lottery_repair_tasks:v1:${platform} " \
    "~adapter_probe_requests:${platform} ~account_calibration_requests:${platform} " \
    "~discovery_scan_requests:v1:${platform} ~discovery_scan_result:v1:*:${platform}")"
  if [ "$platform" = "xiaohongshu" ]; then
    keys="$keys ~xiaohongshu_target_pursuit_requests:v1 ~xiaohongshu_target_pursuit_result:v1:*"
  fi
  printf '%s' "$keys"
}

append_platform_acl_users() {
  platform="$1"
  core_hash_value="$2"
  worker_hash_value="$3"
  suffix="$(printf '%s' "$platform" | tr '[:lower:]' '[:upper:]')"
  eval "core_username=\${REDIS_CORE_${suffix}_USERNAME}"
  eval "worker_username=\${REDIS_WORKER_${suffix}_USERNAME}"
  data_keys="$(platform_data_keys "$platform")"
  printf '%s\n' \
    "user ${core_username} on #${core_hash_value} resetkeys resetchannels -@all +ping +info +multi +exec +discard +select +acl|whoami +acl|dryrun (+eval +xadd +xack +xdel +xlen +xrange +xreadgroup +xclaim +xpending +xinfo ${data_keys} ~dpms:task-stream:*) (+xgroup|delconsumer ~adapter_probe_requests:${platform} ~account_calibration_requests:${platform} ~discovery_scan_requests:v1:${platform}) (+get +set +incr +expire +zadd +zremrangebyscore +zcard ~daily_limit:* ~risk_window:* ~dpms:task-stream:* ~update_signal) (+get +set +del +expire ~discovery_scan_result:v1:*:${platform} ~xiaohongshu_target_pursuit_result:v1:*) (+del ~recovery_count:*) (+publish +subscribe +unsubscribe &structured_logs &worker:reload)" \
    "user ${worker_username} on #${worker_hash_value} resetkeys resetchannels -@all +ping +multi +exec +discard +select +acl|whoami +acl|dryrun (+eval +xack +xdel +xreadgroup +xclaim +xpending +xinfo ${data_keys}) (+xadd ~notify_events ~failed_task_messages ~adapter_probe_requests:${platform} ~account_calibration_requests:${platform}) (+xgroup|delconsumer ${data_keys}) (+get +set +expire ~account_calibration_requeue:* ~xiaohongshu_target_pursuit_result:v1:*) (+del ~account_calibration_requeue:*) (+zadd +zremrangebyscore +zcard +expire ~risk_window:*) (+sadd +sismember +smembers +srem +scard +del ~legacy_task_fanout:*) (+subscribe +unsubscribe &worker:reload)" \
    >>"$acl_tmp"
}

# Phase 2 removes the bootstrap credential. Core has no XGROUP authority.
# Worker retains only DELCONSUMER for bounded stale-identity cleanup. The
# separately configured group-admin has the exact commands needed by the
# approval-bound retirement/sweep tooling and only on governed stream keys.
cat >"$acl_tmp" <<EOF
user default off
user bootstrap off resetpass resetkeys resetchannels -@all
user health on #${health_hash} resetkeys resetchannels -@all +ping +acl|whoami
user core on #${core_hash} resetkeys resetchannels -@all +ping +info +multi +exec +discard +select +acl|whoami +acl|dryrun (+eval +xadd +xack +xdel +xlen +xrange +xreadgroup +xclaim +xpending +xinfo ${runtime_keys}) (+xgroup|delconsumer ~notify_events ~discovery_scan_requests:v1:*) (+get +set +incr +expire +zadd +zremrangebyscore +zcard ~daily_limit:* ~risk_window:* ~dpms:task-stream:* ~update_signal) (+get +set +del +expire ~discovery_scan_result:v1:* ~xiaohongshu_target_pursuit_result:v1:*) (+sadd +sismember +smembers +srem +scard +del ~legacy_task_fanout:*) (+del ~recovery_count:*) (+publish +subscribe +unsubscribe &structured_logs &worker:reload)
user worker on #${worker_hash} resetkeys resetchannels -@all +ping +multi +exec +discard +select +acl|whoami +acl|dryrun (+eval +xack +xdel +xreadgroup +xclaim +xpending +xinfo ${runtime_keys}) (+xadd ~notify_events ~failed_task_messages ~adapter_probe_requests ~adapter_probe_requests:* ~account_calibration_requests ~account_calibration_requests:*) (+xgroup|delconsumer ${governed_stream_keys}) (+get +set +expire ~account_calibration_legacy_fanout:* ~account_calibration_requeue:* ~xiaohongshu_target_pursuit_result:v1:*) (+del ~account_calibration_requeue:*) (+zadd +zremrangebyscore +zcard +expire ~risk_window:*) (+sadd +sismember +smembers +srem +scard +del ~legacy_task_fanout:*) (+subscribe +unsubscribe &worker:reload)
user group-admin on #${group_admin_hash} resetkeys resetchannels -@all +ping +acl|whoami +acl|dryrun (+eval +xinfo|groups +xinfo|consumers +xpending +xrange +xdel +xgroup|destroy +xgroup|delconsumer ${governed_stream_keys})
EOF
append_platform_acl_users bilibili "$core_bilibili_hash" "$worker_bilibili_hash"
append_platform_acl_users weibo "$core_weibo_hash" "$worker_weibo_hash"
append_platform_acl_users xiaohongshu "$core_xiaohongshu_hash" "$worker_xiaohongshu_hash"
append_platform_acl_users douyin "$core_douyin_hash" "$worker_douyin_hash"
mv "$acl_tmp" "$acl_file"

load_result="$(bootstrap_cli ACL LOAD 2>&1)" || true
if [ "$load_result" != "OK" ]; then
  bootstrap_abort "final Redis ACL activation failed"
fi
bootstrap_password=""
bootstrap_hash=""

set +e
wait "$redis_pid"
redis_status="$?"
set -e
exit "$redis_status"
