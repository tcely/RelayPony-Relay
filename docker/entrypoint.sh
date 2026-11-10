#!/bin/sh

set -e

/app/manage.py migrate --noinput

exec "$@"

