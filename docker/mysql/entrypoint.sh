#!/usr/bin/env bash
set -Eeuo pipefail

# Validate the role split and production secrets on every start, including
# existing data volumes for which /docker-entrypoint-initdb.d is not rerun.
DPMS_MYSQL_VALIDATE_ONLY=1 \
  /usr/local/bin/dpms-mysql-provision-roles

# The upstream MySQL entrypoint intentionally calls helper functions without
# positional arguments (for example `_mysql_passfile` during temporary-server
# shutdown). Do not leak this wrapper's nounset option into that script.
set +u
exec /usr/local/bin/docker-entrypoint.sh "$@"
