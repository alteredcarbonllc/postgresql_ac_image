#!/usr/bin/python3
"""Root-owned bridge from a trusted local CI checkout to the existing PG deployer."""
import argparse
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import tarfile
import tempfile

LIB = Path('/usr/local/libexec/ac-postgresql')
STATE = Path('/var/lib/ac-postgresql')
SPOOL = Path('/var/spool/ac-pg-config')
INBOX = SPOOL / 'inbox'
RELEASES = STATE / 'config-releases'
RECEIPTS = STATE / 'config-ci-receipts'
ENABLED = STATE / 'config-ci-enabled'
LAST = STATE / 'config-ci-last.json'
FILES = ('postgresql.conf', 'pg_hba.conf', 'pg_ident.conf')
MAX_BYTES = 4 * 1024 * 1024
BUILDER_UID = 1001

def need(ok, message):
    if not ok:
        raise RuntimeError(message)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def revision(value):
    need(isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value), 'Expected full lowercase commit SHA')
    return value

def safe(path, directory=False):
    st = path.lstat()
    need((stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode))
         and st.st_uid == 0 and not st.st_mode & 0o022, 'Untrusted path: ' + str(path))
    return st

def parents(path):
    for parent in reversed(path.parents):
        safe(parent, True)

def mkdir(path, mode=0o700):
    parents(path)
    if path.exists() or path.is_symlink():
        safe(path, True)
    else:
        path.mkdir(mode=mode)
    path.chmod(mode)

def atomic(path, data, mode=0o600):
    parents(path)
    if path.exists() or path.is_symlink():
        safe(path)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            os.fchmod(out.fileno(), mode)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def save(path, value):
    atomic(path, (json.dumps(value, sort_keys=True, indent=2) + '\n').encode())

def load(path):
    parents(path)
    safe(path)
    return json.loads(path.read_text())

def engine():
    """Pin both installed implementations before importing privileged Python."""
    expected = json.loads(Path(__file__).with_name('engine-sha256.json').read_text())
    for name, digest in expected.items():
        p = LIB / name
        parents(p)
        safe(p)
        need(sha(p.read_bytes()) == digest, 'Installed engine changed: ' + name)
    spec = importlib.util.spec_from_file_location('pg_config_deploy', LIB / 'config-deploy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def decode(data):
    need(0 < len(data) <= MAX_BYTES, 'Archive size limit')
    result, seen = {}, set()
    allowed = {'postgresql/' + name for name in FILES}
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        for index, member in enumerate(archive):
            need(index < 16, 'Too many archive entries')
            name = member.name.rstrip('/') if member.isdir() else member.name
            need(name not in seen, 'Duplicate archive entry')
            seen.add(name)
            if member.isdir():
                need(name == 'postgresql', 'Unexpected directory')
                continue
            need(member.isfile() and name in allowed, 'Unexpected archive entry')
            need(not member.mode & 0o7111, 'Executable or special mode refused')
            need(0 <= member.size < 1024 * 1024, 'File size limit')
            stream = archive.extractfile(member)
            content = stream.read(1024 * 1024)
            need(len(content) == member.size and b'\0' not in content, 'Invalid configuration data')
            content.decode('utf-8')
            result[Path(name).name] = content
    need(set(result) == set(FILES), 'Missing configuration files')
    return result

def snapshot(commit):
    parents(INBOX)
    fd = os.open(INBOX, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        need(st.st_uid == BUILDER_UID and stat.S_IMODE(st.st_mode) == 0o700, 'Unexpected inbox owner/mode')
        incoming = os.open(commit + '.tar', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(incoming, 'rb') as stream:
            before = os.fstat(stream.fileno())
            need(stat.S_ISREG(before.st_mode) and before.st_uid == BUILDER_UID
                 and 0 < before.st_size <= MAX_BYTES, 'Invalid input archive')
            data = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            need((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                 (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                 and len(data) == before.st_size, 'Archive changed during import')
            return data
    finally:
        os.close(fd)

def hashes(blobs):
    return {name: sha(blobs[name]) for name in FILES}

def release_bytes(dest):
    parents(dest)
    safe(dest, True)
    need({p.name for p in dest.iterdir()} == set(FILES), 'Unexpected release files')
    result = {}
    for name in FILES:
        safe(dest / name)
        result[name] = (dest / name).read_bytes()
    return result

def receipt(commit):
    value = load(RECEIPTS / (revision(commit) + '.json'))
    need(value.get('revision') == commit and set(value.get('files', {})) == set(FILES), 'Invalid receipt')
    return value

def verify_release(commit):
    value = receipt(commit)
    dest = RELEASES / commit
    need(hashes(release_bytes(dest)) == value['files'], 'Configuration release drift: ' + commit)
    return dest

def adopt(d):
    commit, dest = d.current_release()
    revision(commit)
    need(dest == RELEASES / commit, 'Unexpected active release path')
    value = {'revision': commit, 'files': hashes(release_bytes(dest))}
    path = RECEIPTS / (commit + '.json')
    if path.exists() or path.is_symlink():
        need(load(path) == value, 'Active configuration differs from receipt')
    else:
        save(path, value)
    return commit

def active(d):
    d.guard()
    commit, dest = d.current_release()
    need(dest == verify_release(commit), 'Unexpected active path')
    return commit, dest

def prepare(commit):
    revision(commit)
    blobs = decode(snapshot(commit))
    value = {'revision': commit, 'files': hashes(blobs)}
    record = RECEIPTS / (commit + '.json')
    if record.exists() or record.is_symlink():
        need(load(record) == value, 'Same revision has different content')
    dest = RELEASES / commit
    if dest.exists() or dest.is_symlink():
        need(release_bytes(dest) == blobs, 'Existing release differs')
    else:
        stage = Path(tempfile.mkdtemp(prefix='.ci-prepare-', dir=RELEASES))
        try:
            for name, data in blobs.items():
                atomic(stage / name, data, 0o444)
            stage.chmod(0o755)
            os.rename(stage, dest)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    save(record, value)
    verify_release(commit)
    print('PG_CONFIG_PREPARED: ' + commit, flush=True)
    return dest

def apply(d, commit, dest, report):
    old, current = active(d)
    verify_release(commit)
    d.test_release(dest, report)
    # Check for drift once more after the isolated test, before stopping production.
    active(d)
    verify_release(commit)
    if receipt(old)['files'] == receipt(commit)['files']:
        save(LAST, {'source_revision': commit, 'mounted_revision': old, 'result': 'noop'})
        print('PG_CONFIG_NOOP: ' + commit + '; no restart', flush=True)
        return
    d.deploy(commit, dest, report)
    active(d)
    save(LAST, {'source_revision': commit, 'mounted_revision': commit, 'result': 'deployed'})
    print('PG_CONFIG_CI_DEPLOY_OK: ' + commit, flush=True)

def main():
    need(os.geteuid() == 0, 'Run as root')
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply', 'status', 'enable', 'disable'))
    parser.add_argument('commit', nargs='?')
    args = parser.parse_args()
    need((args.action in ('prepare', 'apply')) == (args.commit is not None), 'Unexpected arguments')
    if args.commit is not None:
        revision(args.commit)
    d = engine()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, d.r.interrupted)
    parents(STATE / 'lock')
    safe(STATE / 'lock')
    with (STATE / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        need(not (d.PENDING.exists() or d.PENDING.is_symlink()), 'Unfinished PostgreSQL deployment; inspect pending-config-deploy.json')
        old, dest = active(d)
        if args.action == 'disable':
            ENABLED.unlink(missing_ok=True)
            print('PG_CONFIG_CI_DISABLED')
        elif args.action == 'enable':
            atomic(ENABLED, b'enabled\n')
            print('PG_CONFIG_CI_ENABLED')
        elif args.action == 'status':
            print('MOUNTED_CONFIG=' + old)
            if LAST.exists():
                print('LAST_CI=' + json.dumps(load(LAST), sort_keys=True))
            print('CI_DEPLOY_ENABLED=' + str(ENABLED.exists()).lower())
        else:
            if args.action == 'apply':
                safe(ENABLED)
            dest = prepare(args.commit)
            report = Path(tempfile.mkdtemp(prefix='config-ci.', dir=STATE))
            save(report / 'release.json', receipt(args.commit))
            print('REPORT=' + str(report), flush=True)
            try:
                if args.action == 'apply':
                    apply(d, args.commit, dest, report)
                else:
                    d.test_release(dest, report)
                    print('PG_CONFIG_PREFLIGHT_OK: production unchanged')
            except BaseException as exc:
                import traceback
                detail = traceback.format_exc()
                detail += '\nSTDOUT:\n' + str(getattr(exc, 'stdout', '') or '')
                detail += '\nSTDERR:\n' + str(getattr(exc, 'stderr', '') or '')
                atomic(report / 'error.txt', detail.encode())
                raise

if __name__ == '__main__':
    try:
        main()
    except BaseException as exc:
        # Do not expose PostgreSQL diagnostics/config contents to CI logs.
        print('PG_CONFIG_CI_ERROR: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
