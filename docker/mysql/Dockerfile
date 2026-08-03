FROM mysql:8.0@sha256:62fb722c78b24245ddff1796a0fcee4a49cc5b87e0aaaf20c92d1da9e0a2497b

COPY --chmod=0755 docker/mysql/provision-roles.sh \
    /usr/local/bin/dpms-mysql-provision-roles
COPY --chmod=0755 docker/mysql/entrypoint.sh \
    /usr/local/bin/dpms-mysql-entrypoint
COPY --chmod=0644 docker/mysql/provision-roles.sh \
    /docker-entrypoint-initdb.d/001-provision-roles.sh
COPY --chmod=0644 docker/mysql/001-bootstrap.sql \
    /docker-entrypoint-initdb.d/010-bootstrap.sql

ENTRYPOINT ["/usr/local/bin/dpms-mysql-entrypoint"]
