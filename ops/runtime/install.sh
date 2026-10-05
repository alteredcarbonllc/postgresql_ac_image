#!/bin/sh
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run as root' >&2; exit 1; }
[ "$#" = 0 ] || { echo 'Usage: install.sh' >&2; exit 1; }
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for tool in /usr/bin/python3 /usr/bin/runsv /usr/bin/sv /usr/bin/svlogd /usr/bin/systemctl /usr/bin/pgrep /usr/local/bin/podman; do
    [ -x "$tool" ] || { echo "Missing $tool" >&2; exit 1; }
done
# v1 is a one-time installer, not a host-tool updater.
for path in /var/lib/ac-postgresql /etc/ac/runit/postgresql /usr/local/libexec/ac-postgresql /etc/systemd/system/ac-postgresql-runit.service /usr/local/sbin/ac-pgctl; do
    if [ -e "$path" ] || [ -L "$path" ]; then
        echo "Already exists: $path; refusing overwrite" >&2
        exit 1
    fi
done
python3 - "$base" <<'PY'
import ast, pathlib, sys
root=pathlib.Path(sys.argv[1])
ast.parse((root/'ac-pg-runtime.py').read_text())
for p in ['/var/lib','/etc/ac','/etc/ac/runit','/etc/ac/managed','/usr/local/libexec','/usr/local/sbin','/etc/systemd/system']:
    q=pathlib.Path(p)
    if q.is_symlink(): raise SystemExit('Refusing symlink '+p)
    if q.exists():
        s=q.stat()
        if not q.is_dir() or s.st_uid!=0 or s.st_mode & 0o022:
            raise SystemExit('Unsafe directory '+p)
PY
install -d -o root -g root -m 0700 /var/lib/ac-postgresql
install -d -o root -g root -m 0755 /usr/local/libexec/ac-postgresql /usr/local/libexec/ac-postgresql/legacy /etc/ac/managed
install -d -o root -g root -m 0755 /etc/ac/runit/postgresql /etc/ac/runit/postgresql/control /etc/ac/runit/postgresql/log
install -d -o root -g root -m 0700 /var/log/ac-postgresql-supervisor
install -m 0644 -o root -g root "$base/ac-pg-runtime.py" /usr/local/libexec/ac-postgresql/ac-pg-runtime.py
for file in start.before start.after stop.before stop.after; do
    install -m 0644 -o root -g root "$base/legacy/$file" /usr/local/libexec/ac-postgresql/legacy/"$file"
done
for file in run finish control/t log/run; do
    install -m 0755 -o root -g root "$base/runit/$file" /etc/ac/runit/postgresql/"$file"
done
install -m 0755 -o root -g root "$base/ac-pgctl" /usr/local/sbin/ac-pgctl
install -m 0644 -o root -g root /dev/null /etc/ac/runit/postgresql/down
install -m 0644 -o root -g root "$base/ac-postgresql-runit.service" /etc/systemd/system/ac-postgresql-runit.service
systemctl daemon-reload
echo 'INSTALLED_DOWN: no container changes, no supervisor started'
