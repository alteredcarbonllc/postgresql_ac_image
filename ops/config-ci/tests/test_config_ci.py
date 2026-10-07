import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pg_config_ci', HERE / 'pg_config_ci.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
sys.modules['pg_config_ci'] = c
spec = importlib.util.spec_from_file_location('ci_installer', HERE / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

BLOBS = {name: ('# ' + name + '\n').encode() for name in c.FILES}
A, B = 'a' * 40, 'b' * 40

def archive(blobs=BLOBS, extra=()):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as tar:
        for name, data in blobs.items():
            member = tarfile.TarInfo('postgresql/' + name)
            member.mode = 0o644
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
        for member, data in extra:
            tar.addfile(member, io.BytesIO(data))
    return out.getvalue()

class ArchiveTests(unittest.TestCase):
    def test_exact_files(self):
        self.assertEqual(c.decode(archive()), BLOBS)

    def test_missing_file(self):
        with self.assertRaises(RuntimeError):
            c.decode(archive({'postgresql.conf': b''}))

    def test_duplicate(self):
        member = tarfile.TarInfo('postgresql/pg_hba.conf')
        with self.assertRaises(RuntimeError):
            c.decode(archive(extra=[(member, b'')]))

    def test_links_paths_unknown_and_executable(self):
        for name, kind, mode in [
            ('../x', tarfile.REGTYPE, 0o644),
            ('/postgresql/pg_hba.conf', tarfile.REGTYPE, 0o644),
            ('postgresql/./pg_hba.conf', tarfile.REGTYPE, 0o644),
            ('postgresql/extra.conf', tarfile.REGTYPE, 0o644),
            ('postgresql/postgresql.conf', tarfile.SYMTYPE, 0o644),
            ('postgresql/postgresql.conf', tarfile.LNKTYPE, 0o644),
            ('postgresql/postgresql.conf', tarfile.FIFOTYPE, 0o644),
            ('postgresql/postgresql.conf', tarfile.REGTYPE, 0o755),
        ]:
            with self.subTest(name=name, kind=kind, mode=mode):
                member = tarfile.TarInfo(name)
                member.type, member.mode = kind, mode
                with self.assertRaises(RuntimeError):
                    c.decode(archive({}, [(member, b'')]))

    def test_size_and_invalid_text(self):
        for data in (b'\0', b'\xff', b'x' * (1024 * 1024)):
            with self.assertRaises((RuntimeError, UnicodeError)):
                c.decode(archive({**BLOBS, 'postgresql.conf': data}))
        with self.assertRaises(RuntimeError):
            c.decode(b'x' * (c.MAX_BYTES + 1))

class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.oldmask = os.umask(0o077)
        self.addCleanup(os.umask, self.oldmask)
        self.patches = []
        for key, value in {
            'STATE': self.base / 'state',
            'SPOOL': self.base / 'spool',
            'INBOX': self.base / 'spool/inbox',
            'RELEASES': self.base / 'state/config-releases',
            'RECEIPTS': self.base / 'state/config-ci-receipts',
            'LAST': self.base / 'state/last.json',
            'ENABLED': self.base / 'state/enabled',
            'BUILDER_UID': os.getuid(),
        }.items():
            p = patch.object(c, key, value)
            p.start()
            self.addCleanup(p.stop)
        # Owner check adapts to unprivileged laptop tests; retain type/mode checks.
        def safe(path, directory=False):
            st = path.lstat()
            c.need((stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode))
                   and st.st_uid == os.getuid() and not st.st_mode & 0o022, 'Unsafe test path')
            return st
        p = patch.object(c, 'safe', side_effect=safe)
        p.start()
        self.addCleanup(p.stop)
        # System /tmp itself is world writable; only fixture descendants are checked.
        def parents(path):
            for parent in reversed(path.parents):
                if parent == self.base or self.base in parent.parents:
                    safe(parent, True)
        p = patch.object(c, 'parents', side_effect=parents)
        p.start()
        self.addCleanup(p.stop)
        for path in (c.STATE, c.SPOOL, c.INBOX, c.RELEASES, c.RECEIPTS):
            path.mkdir(mode=0o700)
        (c.STATE / 'lock').touch(mode=0o600)

    def import_release(self, commit=A, blobs=BLOBS):
        (c.INBOX / (commit + '.tar')).write_bytes(archive(blobs))
        return c.prepare(commit)

    def test_import_repeat_and_modes(self):
        dest = self.import_release()
        self.assertEqual(self.import_release(), dest)
        self.assertEqual(c.release_bytes(dest), BLOBS)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o755)
        for name in c.FILES:
            self.assertEqual(stat.S_IMODE((dest / name).stat().st_mode), 0o444)

    def test_same_revision_different_content(self):
        self.import_release()
        with self.assertRaisesRegex(RuntimeError, 'Same revision'):
            self.import_release(blobs={**BLOBS, 'postgresql.conf': b'# new\n'})

    def test_drift_and_symlink_refused(self):
        dest = self.import_release()
        p = dest / 'postgresql.conf'
        p.chmod(0o600)
        p.write_text('# drift')
        with self.assertRaisesRegex(RuntimeError, 'drift'):
            c.verify_release(A)
        p.unlink()
        p.symlink_to(dest / 'pg_hba.conf')
        with self.assertRaises(RuntimeError):
            c.verify_release(A)

    def test_inbox_link_and_fifo_refused(self):
        incoming = c.INBOX / (A + '.tar')
        other = self.base / 'archive.tar'
        other.write_bytes(archive())
        incoming.symlink_to(other)
        with self.assertRaises(OSError):
            c.snapshot(A)
        incoming.unlink()
        os.mkfifo(incoming, 0o600)
        with self.assertRaises(RuntimeError):
            c.snapshot(A)

    def test_mkdir_new_existing_umask_and_link(self):
        p = self.base / 'public'
        c.mkdir(p, 0o755)
        self.assertEqual(p.stat().st_mode & 0o777, 0o755)
        p.chmod(0o700)
        c.mkdir(p, 0o755)
        self.assertEqual(p.stat().st_mode & 0o777, 0o755)
        link = self.base / 'link'
        link.symlink_to(p)
        with self.assertRaises(RuntimeError):
            c.mkdir(link, 0o755)

    def fake_engine(self):
        self.current = A
        d = SimpleNamespace()
        d.guard = Mock()
        d.current_release = lambda: (self.current, c.RELEASES / self.current)
        d.test_release = Mock()
        def deploy(commit, dest, report):
            self.current = commit
        d.deploy = Mock(side_effect=deploy)
        return d

    def test_noop_preserves_mounted_revision(self):
        self.import_release(A)
        dest = self.import_release(B)
        d = self.fake_engine()
        c.apply(d, B, dest, self.base)
        d.test_release.assert_called_once()
        d.deploy.assert_not_called()
        self.assertEqual(c.load(c.LAST), {'source_revision': B, 'mounted_revision': A, 'result': 'noop'})

    def test_changed_content_uses_existing_deployer(self):
        self.import_release(A)
        dest = self.import_release(B, {**BLOBS, 'postgresql.conf': b'# new\n'})
        d = self.fake_engine()
        c.apply(d, B, dest, self.base)
        d.deploy.assert_called_once_with(B, dest, self.base)
        self.assertEqual(c.load(c.LAST)['mounted_revision'], B)

    def test_failed_preflight_never_deploys(self):
        self.import_release(A)
        dest = self.import_release(B, {**BLOBS, 'postgresql.conf': b'# new\n'})
        d = self.fake_engine()
        d.test_release.side_effect = RuntimeError('invalid configuration')
        with self.assertRaises(RuntimeError):
            c.apply(d, B, dest, self.base)
        d.deploy.assert_not_called()
        self.assertFalse(c.LAST.exists())

    def test_failed_deployment_never_records_success(self):
        self.import_release(A)
        dest = self.import_release(B, {**BLOBS, 'postgresql.conf': b'# new\n'})
        d = self.fake_engine()
        d.deploy.side_effect = RuntimeError('rolled back by original engine')
        with self.assertRaises(RuntimeError):
            c.apply(d, B, dest, self.base)
        self.assertFalse(c.LAST.exists())

    def test_active_drift_before_test(self):
        self.import_release(A)
        dest = self.import_release(B)
        p = c.RELEASES / A / 'pg_hba.conf'
        p.chmod(0o600)
        p.write_text('# changed')
        d = self.fake_engine()
        with self.assertRaises(RuntimeError):
            c.apply(d, B, dest, self.base)
        d.test_release.assert_not_called()
        d.deploy.assert_not_called()

    def test_adopt_does_not_replace_baseline(self):
        self.import_release(A)
        d = self.fake_engine()
        self.assertEqual(c.adopt(d), A)
        p = c.RELEASES / A / 'pg_hba.conf'
        p.chmod(0o600)
        p.write_text('# changed')
        with self.assertRaises(RuntimeError):
            c.adopt(d)

    def test_installer_runs_twice_without_restart_and_preserves_enable(self):
        self.import_release(A)
        d = self.fake_engine()
        d.PENDING = c.STATE / 'pending-config-deploy.json'
        d.r = SimpleNamespace()
        enabled = c.ENABLED
        enabled.write_text('enabled\n')
        target = self.base / 'libexec'
        # Run actual installer logic, remapping only fixed host destinations.
        original_path = Path
        def mapped_path(value):
            if value == '/usr/local/sbin/ac-pg-config-ci':
                return self.base / 'bin/ac-pg-config-ci'
            if value == '/etc/sudoers.d/ac-pg-config-ci':
                return self.base / 'sudoers/ac-pg-config-ci'
            return original_path(value)
        (self.base / 'bin').mkdir()
        (self.base / 'sudoers').mkdir()
        account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        with patch.object(installer, 'TARGET', target), patch.object(installer, 'Path', side_effect=mapped_path), \
             patch.object(c, 'engine', return_value=d), patch.object(installer.os, 'geteuid', return_value=0), \
             patch.object(installer.pwd, 'getpwnam', return_value=account), \
             patch.object(installer.subprocess, 'run') as run:
            installer.install()
            installer.install()
        self.assertTrue(enabled.exists())
        self.assertEqual(c.SPOOL.stat().st_mode & 0o777, 0o755)
        self.assertEqual(c.INBOX.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(call.args[0][0] == '/usr/sbin/visudo' for call in run.call_args_list))
        self.assertEqual(run.call_count, 4)
        d.deploy.assert_not_called()
        d.test_release.assert_not_called()

if __name__ == '__main__':
    unittest.main()
