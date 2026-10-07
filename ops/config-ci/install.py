#!/usr/bin/python3
"""Install configuration CI bridge; no runtime modification and no service restart."""
import fcntl
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pg_config_ci as c
HERE = Path(__file__).resolve().parent
TARGET = Path('/usr/local/libexec/ac-pg-config-ci')

def install():
    c.need(os.geteuid() == 0, 'Run as root')
    os.umask(0o077)
    account = pwd.getpwnam('ac-ci-builder')
    c.need(account.pw_uid == c.BUILDER_UID, 'Unexpected builder UID')
    d = c.engine()
    c.parents(c.STATE / 'lock')
    c.safe(c.STATE / 'lock')
    with (c.STATE / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        c.need(not (d.PENDING.exists() or d.PENDING.is_symlink()), 'Pending PostgreSQL deployment')
        d.guard()
        c.mkdir(c.RECEIPTS)
        c.adopt(d)
        c.mkdir(c.SPOOL, 0o755)
        if c.INBOX.exists() or c.INBOX.is_symlink():
            st = c.INBOX.lstat()
            c.need(not c.INBOX.is_symlink() and c.INBOX.is_dir() and
                   st.st_uid == account.pw_uid and st.st_mode & 0o777 == 0o700, 'Unexpected inbox')
        else:
            c.INBOX.mkdir(mode=0o700)
            os.chown(c.INBOX, account.pw_uid, account.pw_gid)
        queue = c.SPOOL / 'queue.lock'
        if queue.exists() or queue.is_symlink():
            st = queue.lstat()
            import stat
            c.need(stat.S_ISREG(st.st_mode) and st.st_uid == account.pw_uid
                   and st.st_mode & 0o777 == 0o600, 'Unexpected queue lock')
        else:
            fd = os.open(queue, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchown(fd, account.pw_uid, account.pw_gid)
            os.close(fd)
        c.mkdir(TARGET, 0o755)
        files = {TARGET / name: (HERE / name, 0o644)
                 for name in ('pg_config_ci.py', 'engine-sha256.json')}
        files[Path('/usr/local/sbin/ac-pg-config-ci')] = (HERE / 'ac-pg-config-ci', 0o755)
        rules = ''.join('ac-ci-builder ALL=(root) NOPASSWD: /usr/local/sbin/ac-pg-config-ci ' + action + ' *\n'
                        for action in ('prepare', 'apply')).encode()
        sudoers = Path('/etc/sudoers.d/ac-pg-config-ci')
        for path, (source, _) in files.items():
            c.parents(path)
            if path.exists() or path.is_symlink():
                c.safe(path)
                c.need(path.read_bytes() == source.read_bytes(), 'Existing tool differs: ' + str(path))
        c.parents(sudoers)
        if sudoers.exists() or sudoers.is_symlink():
            c.safe(sudoers)
            c.need(sudoers.read_bytes() == rules, 'Existing sudoers differs')
        report = Path(tempfile.mkdtemp(prefix='config-ci-install.', dir=c.STATE))
        staged = report / 'sudoers.new'
        c.atomic(staged, rules, 0o440)
        subprocess.run(['/usr/sbin/visudo', '-cf', str(staged)], check=True)
        for path, (source, mode) in files.items():
            c.atomic(path, source.read_bytes(), mode)
        c.atomic(sudoers, rules, 0o440)
        subprocess.run(['/usr/sbin/visudo', '-c'], check=True)
        print('PG_CONFIG_CI_INSTALLED: no restart; report=' + str(report))
        print('CI_DEPLOY_ENABLED=' + str(c.ENABLED.exists()).lower())

if __name__ == '__main__':
    try:
        install()
    except Exception as exc:
        print('INSTALL_ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)
