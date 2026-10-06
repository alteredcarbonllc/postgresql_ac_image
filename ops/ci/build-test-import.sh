#!/bin/sh
set -eu
: "${CI_COMMIT_SHA:?}"
[ "${#CI_COMMIT_SHA}" = 40 ]
case "$CI_COMMIT_SHA" in *[!0-9a-f]*) exit 1;; esac
test "$(id -u)" = 1001
pod() { /usr/local/bin/podman --remote=false "$@"; }
test "$(pod info --format '{{.Host.Security.Rootless}}')" = true
image="localhost/postgresql-ac:$CI_COMMIT_SHA"
if pod image exists "$image"; then
    echo "REUSE_CANDIDATE: $image"
else
    pod build --network=slirp4netns \
      --label "org.opencontainers.image.revision=$CI_COMMIT_SHA" \
      -f Containerfile.ci -t "$image" .
fi
python3 ops/ci/test-image.py "$image" "$CI_COMMIT_SHA"
sh ops/ci/export-image.sh
echo "PG_CANDIDATE_READY: $CI_COMMIT_SHA; production unchanged"
