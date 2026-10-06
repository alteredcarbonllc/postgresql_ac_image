#!/bin/sh
set -eu
revision=${CI_COMMIT_SHA:?CI_COMMIT_SHA is required}
[ "${#revision}" -eq 40 ] || { echo 'Expected full commit SHA' >&2; exit 1; }
case "$revision" in *[!0-9a-f]*) echo 'Invalid commit SHA' >&2; exit 1;; esac
[ "$(id -u)" -eq 1001 ] || { echo 'Expected ac-ci-builder UID 1001' >&2; exit 1; }
export XDG_DATA_HOME=/var/lib/ac-ci-builder/.local/share
export XDG_CONFIG_HOME=/var/lib/ac-ci-builder/.config
export XDG_RUNTIME_DIR=/run/user/1001
unset CONTAINER_HOST CONTAINER_CONNECTION CONTAINERS_STORAGE_CONF
inbox=/var/spool/ac-postgresql/inbox
image="localhost/postgresql-ac:$revision"
archive="$inbox/$revision.tar"
umask 077
temporary=$(mktemp "$inbox/.export.XXXXXXXX")
cleanup() { rm -f -- "$temporary"; }
trap cleanup EXIT
/usr/local/bin/podman --remote=false save --format docker-archive --output "$temporary" "$image"
mv -f -- "$temporary" "$archive"
sudo -n /usr/local/sbin/ac-postgresql-import "$revision"
rm -f -- "$archive"
