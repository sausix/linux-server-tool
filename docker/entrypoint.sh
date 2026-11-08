#!/bin/sh
set -eu
# Private databases and files; listener socket mode is set explicitly by the backend.
umask 0077
exec "$@"
