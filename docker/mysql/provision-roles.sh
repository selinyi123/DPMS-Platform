#!/usr/bin/env bash
set -Eeuo pipefail

runtime_user="${MYSQL_RUNTIME_USER:-dpms_runtime}"
migration_user="${MYSQL_MIGRATION_USER:-dpms_migrate}"
database_name="${MYSQL_DATABASE:-lottery}"
runtime_password="${MYSQL_RUNTIME_PASSWORD:-}"
migration_password="${MYSQL_MIGRATION_PASSWORD:-}"
deployment_mode="${DEPLOYMENT_MODE:-dev}"
root_password="${MYSQL_ROOT_PASSWORD:-}"
platform_database_mode="${DPMS_MYSQL_PLATFORM_DATABASE_MODE:-shared}"

identifier_pattern='^[A-Za-z0-9_]{1,32}$'
# These values are interpolated into mysql+aiomysql URLs by Compose. Restrict
# them to RFC 3986 unreserved characters so the provisioned password and URL
# parser cannot disagree about reserved punctuation such as @, :, /, or %.
password_pattern='^[A-Za-z0-9._~-]{16,128}$'
default_runtime_password='dpms-runtime-local-only-change-me-2026'
default_migration_password='dpms-migrate-local-only-change-me-2026'

if [[ ! "${runtime_user}" =~ ${identifier_pattern} ]]; then
  echo "mysql_runtime_user_invalid" >&2
  return 1 2>/dev/null || exit 1
fi
if [[ ! "${migration_user}" =~ ${identifier_pattern} ]]; then
  echo "mysql_migration_user_invalid" >&2
  return 1 2>/dev/null || exit 1
fi
if [[ "${runtime_user}" == "${migration_user}" ]]; then
  echo "mysql_roles_must_be_distinct" >&2
  return 1 2>/dev/null || exit 1
fi
if [[ ! "${database_name}" =~ ${identifier_pattern} ]]; then
  echo "mysql_database_name_invalid" >&2
  return 1 2>/dev/null || exit 1
fi
if [[ "${platform_database_mode}" != "shared" && "${platform_database_mode}" != "isolated" ]]; then
  echo "mysql_platform_database_mode_invalid" >&2
  return 1 2>/dev/null || exit 1
fi
if [[ ! "${runtime_password}" =~ ${password_pattern} ]]; then
  echo "mysql_runtime_password_invalid" >&2
  return 1 2>/dev/null || exit 1
fi
if [[ ! "${migration_password}" =~ ${password_pattern} ]]; then
  echo "mysql_migration_password_invalid" >&2
  return 1 2>/dev/null || exit 1
fi
if [[ "${runtime_password}" == "${migration_password}" ]]; then
  echo "mysql_role_passwords_must_be_distinct" >&2
  return 1 2>/dev/null || exit 1
fi
if [[ "${deployment_mode,,}" == "production" ]]; then
  if [[ "${platform_database_mode}" != "isolated" ]]; then
    echo "mysql_platform_database_mode_isolated_required_in_production" >&2
    return 1 2>/dev/null || exit 1
  fi
  if [[ "${runtime_password}" == "${default_runtime_password}" ]]; then
    echo "mysql_runtime_password_is_development_default" >&2
    return 1 2>/dev/null || exit 1
  fi
  if [[ "${migration_password}" == "${default_migration_password}" ]]; then
    echo "mysql_migration_password_is_development_default" >&2
    return 1 2>/dev/null || exit 1
  fi
  if [[ -z "${root_password}" || "${root_password}" == "rootpass" || ${#root_password} -lt 16 ]]; then
    echo "mysql_root_password_is_missing_default_or_short" >&2
    return 1 2>/dev/null || exit 1
  fi
fi

if [[ "${DPMS_MYSQL_VALIDATE_ONLY:-0}" == "1" ]]; then
  echo "mysql_role_environment_validated"
  return 0 2>/dev/null || exit 0
fi

provision_sql() {
  if declare -F docker_process_sql >/dev/null 2>&1; then
    docker_process_sql --database=mysql
    return
  fi
  if [[ -z "${MYSQL_ROOT_PASSWORD:-}" ]]; then
    echo "mysql_root_password_required" >&2
    return 1
  fi
  if [[ -n "${MYSQL_ADMIN_HOST:-}" ]]; then
    MYSQL_PWD="${MYSQL_ROOT_PASSWORD}" \
      mysql \
        --protocol=tcp \
        --host="${MYSQL_ADMIN_HOST}" \
        --port="${MYSQL_ADMIN_PORT:-3306}" \
        --user=root \
        --database=mysql
    return
  fi
  MYSQL_PWD="${MYSQL_ROOT_PASSWORD}" \
    mysql --protocol=socket --user=root --database=mysql
}

provision_sql <<SQL
CREATE USER IF NOT EXISTS '${runtime_user}'@'%' IDENTIFIED BY '${runtime_password}';
ALTER USER '${runtime_user}'@'%' IDENTIFIED BY '${runtime_password}';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM '${runtime_user}'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE
  ON \`${database_name}\`.* TO '${runtime_user}'@'%';

CREATE USER IF NOT EXISTS '${migration_user}'@'%' IDENTIFIED BY '${migration_password}';
ALTER USER '${migration_user}'@'%' IDENTIFIED BY '${migration_password}';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM '${migration_user}'@'%';
GRANT ALL PRIVILEGES ON \`${database_name}\`.*
  TO '${migration_user}'@'%';
SQL

platform_user_sql() {
  local platform="$1"
  local user="$2"
  local password="$3"
  local platform_database="$4"
  cat <<SQL
CREATE USER IF NOT EXISTS '${user}'@'%' IDENTIFIED BY '${password}';
ALTER USER '${user}'@'%' IDENTIFIED BY '${password}';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM '${user}'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE
  ON \`${platform_database}\`.* TO '${user}'@'%';
SQL
}

for platform in bilibili weibo xiaohongshu douyin; do
  suffix="${platform^^}"
  eval "platform_user=\${MYSQL_RUNTIME_USER_${suffix}:-dpms_runtime_${platform}}"
  eval "platform_password=\${MYSQL_RUNTIME_PASSWORD_${suffix}:-dpms-runtime-${platform}-local-only-change-me-2026}"
  eval "platform_database=\${MYSQL_DATABASE_${suffix}:-${database_name}_${platform}}"
  if [[ ! "${platform_user}" =~ ${identifier_pattern} ]]; then
    echo "mysql_platform_runtime_user_invalid:${platform}" >&2
    return 1 2>/dev/null || exit 1
  fi
  if [[ ! "${platform_database}" =~ ${identifier_pattern} ]]; then
    echo "mysql_platform_database_name_invalid:${platform}" >&2
    return 1 2>/dev/null || exit 1
  fi
  if [[ ! "${platform_password}" =~ ${password_pattern} ]]; then
    echo "mysql_platform_runtime_password_invalid:${platform}" >&2
    return 1 2>/dev/null || exit 1
  fi
  if [[ "${platform_user}" == "${runtime_user}" || "${platform_user}" == "${migration_user}" ]]; then
    echo "mysql_platform_runtime_user_reuses_control_role:${platform}" >&2
    return 1 2>/dev/null || exit 1
  fi
  case "${seen_platform_users:-|}" in
    *"|${platform_user}|"*)
      echo "mysql_platform_runtime_users_must_be_distinct" >&2
      return 1 2>/dev/null || exit 1
      ;;
  esac
  seen_platform_users="${seen_platform_users:-|}${platform_user}|"
  if [[ "${deployment_mode,,}" == "production" ]]; then
    case "${seen_platform_passwords:-|}" in
      *"|${platform_password}|"*)
        echo "mysql_platform_runtime_passwords_must_be_distinct" >&2
        return 1 2>/dev/null || exit 1
        ;;
    esac
    seen_platform_passwords="${seen_platform_passwords:-|}${platform_password}|"
  fi
  if [[ "${platform_database_mode}" == "isolated" ]]; then
    if [[ "${platform_database}" == "${database_name}" ]]; then
      echo "mysql_platform_database_must_not_reuse_control_database:${platform}" >&2
      return 1 2>/dev/null || exit 1
    fi
    case "${seen_platform_databases:-|}" in
      *"|${platform_database}|"*)
        echo "mysql_platform_databases_must_be_distinct" >&2
        return 1 2>/dev/null || exit 1
        ;;
    esac
    seen_platform_databases="${seen_platform_databases:-|}${platform_database}|"
    provision_sql <<SQL
CREATE DATABASE IF NOT EXISTS \`${platform_database}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
GRANT ALL PRIVILEGES ON \`${platform_database}\`.*
  TO '${migration_user}'@'%';
SQL
  else
    platform_database="${database_name}"
  fi
  platform_user_sql "$platform" "$platform_user" "$platform_password" "$platform_database" | provision_sql
done

echo "mysql_dpms_roles_provisioned"
