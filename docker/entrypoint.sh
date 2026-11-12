#!/usr/bin/env sh

set -eu

# Maintain ownership
if [ 0 -eq $(id -u) ]
then
    next_user='app'
    find /app \
        -mindepth 1 -maxdepth 1 -xdev \
        '(' -name data -o -name .venv ')' -prune -o \
        -exec chown --recursive root:root '{}' +
    chown --changes --recursive "${next_user}:${next_user}" \
        /app/data /app/.venv
    user_home="$(getent passwd "${next_user}" | cut -d : -f 6)"
    HOME="${user_home}" \
    su --preserve-environment -s /app/entrypoint.sh -- "${next_user}" "$@"
fi

/app/manage.py createcachetable

/app/manage.py migrate --noinput

exec "$@"

