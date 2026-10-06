import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

BASE=Path(__file__).resolve().parents[1]
def module():
    s=importlib.util.spec_from_file_location('deploy',BASE/'config-deploy.py')
    m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
    return m

class Tests(unittest.TestCase):
    def setUp(self):
        self.m=module()
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.m.r.STATE=self.root
        self.m.r.SERVICE=self.root/'service';self.m.r.SERVICE.mkdir()
        self.m.ACTIVE=self.root/'active.json'
        self.m.PENDING=self.root/'pending.json'
        self.old={'id':'a'*64,'image':self.m.r.IMAGE,'cmd':['postgres'],
                  'mounts':{'/var/lib/postgresql/data':'/data','/var/log':'/logs'},'config_commit':None}
        self.m.r.MOUNTS=self.old['mounts']
        self.m.save(self.m.ACTIVE,self.old)
    def test_environment_order_and_values(self):
        self.assertEqual(self.m.env_map(['A=1','B=x=y']),self.m.env_map(['B=x=y','A=1']))
        self.assertNotEqual(self.m.env_map(['A=1']),self.m.env_map(['A=2']))
        self.assertNotEqual(self.m.env_map(['A=']),self.m.env_map([]))
        for items in (['A=1','A=2'], ['A'], ['=value']):
            with self.assertRaises(RuntimeError): self.m.env_map(items)

    def test_sql_exceptions_only_for_test(self):
        self.assertIn('setting could not be applied',self.m.config_sql(True))
        self.assertNotIn('setting could not be applied',self.m.config_sql(False))
    def test_create_preserves_data_and_has_no_replace(self):
        old={'Config':{'Hostname':'oldhost','Env':['A=B']}}
        a=self.m.create_args(old,Path('/release'),'candidate')
        self.assertNotIn('--replace',a)
        self.assertIn('/data:/var/lib/postgresql/data:Z',a)
        self.assertIn('/release:/etc/postgresql:ro',a)
        self.assertIn('--stop-signal=SIGINT',a)
        self.assertIn('--shm-size=65536000b',a)
        self.assertIn('--ulimit=nproc=4194304:4194304',a)
    def test_shutdown_never_force_kills(self):
        c={'HostConfig':{'RestartPolicy':{'Name':'no'}},'State':{'Running':True}}
        with patch.object(self.m,'byid',return_value=c),patch.object(self.m.r,'pod') as pod,patch.object(self.m.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'no SIGKILL'): self.m.shutdown('id')
        self.assertEqual(pod.call_args.args,('kill','--signal=SIGINT','id'))
        self.assertEqual(pod.call_count,1)
    def test_failed_candidate_can_be_stopped_without_blocking_rollback(self):
        c={'HostConfig':{'RestartPolicy':{'Name':'no'}},'State':{'Running':False,'Status':'exited','ExitCode':1}}
        with patch.object(self.m,'byid',return_value=c):
            with self.assertRaises(RuntimeError): self.m.shutdown('id')
            self.m.shutdown('id',allow_failed=True)
    def test_candidate_manifest_never_accepts_wrong_id(self):
        self.m.r.load_active()
        with self.assertRaisesRegex(RuntimeError,'Unexpected container/image'):
            self.m.r.verify({'Id':'b'*64},True)
    def test_git_release_import_and_drift_rejection(self):
        repo=self.root/'repo';repo.mkdir()
        def git(*a): subprocess.run(['git','-C',str(repo),*a],check=True,capture_output=True)
        git('init','-b','main');git('config','user.name','Test');git('config','user.email','test@example.invalid')
        (repo/'postgresql').mkdir()
        for n in self.m.FILES: (repo/'postgresql'/n).write_text('# test\n')
        git('add','.');git('commit','-m','test')
        rev,dest=self.m.prepare(repo)
        self.assertEqual(len(rev),40)
        self.assertEqual((dest/'postgresql.conf').stat().st_mode & 0o777,0o444)
        self.assertEqual(self.m.prepare(repo),(rev,dest))
        (dest/'postgresql.conf').chmod(0o644)
        (dest/'postgresql.conf').write_text('changed')
        with self.assertRaisesRegex(RuntimeError,'Release changed'): self.m.prepare(repo)
    def test_git_symlink_rejected(self):
        repo=self.root/'repo';repo.mkdir()
        def git(*a): subprocess.run(['git','-C',str(repo),*a],check=True,capture_output=True)
        git('init','-b','main');git('config','user.name','Test');git('config','user.email','test@example.invalid')
        (repo/'postgresql').mkdir()
        (repo/'postgresql'/'postgresql.conf').symlink_to('/etc/passwd')
        git('add','.');git('commit','-m','test')
        with self.assertRaisesRegex(RuntimeError,'regular'): self.m.prepare(repo)
    def test_rollback_never_starts_old_before_candidate_stops(self):
        events=[]
        candidate={'Id':'b'*64,'Image':self.m.r.IMAGE}
        record={'old':self.old,'candidate':'b'*64,'report':str(self.root/'transaction')}
        def pod(*a,**kw):
            events.append(a)
            return subprocess.CompletedProcess(a,0,'','')
        with patch.object(self.m.r,'pod',side_effect=pod),patch.object(self.m.r,'inspect',return_value=candidate),patch.object(self.m,'byid',return_value={'Name':'backup','State':{'Running':False}}),patch.object(self.m,'shutdown',side_effect=lambda *a,**kw:events.append(('shutdown',*a))),patch.object(self.m,'activate',side_effect=lambda a:events.append(('activate',a['id']))),patch.object(self.m.r,'run',side_effect=lambda a,**kw:events.append(tuple(a))),patch.object(self.m.r,'healthy'):
            self.m.rollback(record)
        stop=events.index(('shutdown','b'*64))
        rename=events.index(('rename','a'*64,self.m.r.NAME))
        start=events.index(('/usr/bin/sv','-w','140','up',str(self.m.r.SERVICE)))
        self.assertLess(stop,rename);self.assertLess(rename,start)
    def test_failed_shutdown_does_not_activate_old(self):
        record={'old':self.old,'candidate':'b'*64,'report':str(self.root/'transaction')}
        with patch.object(self.m.r,'run'),patch.object(self.m.r,'pod',return_value=subprocess.CompletedProcess([],0)),patch.object(self.m.r,'inspect',return_value={'Id':'b'*64,'Image':self.m.r.IMAGE}),patch.object(self.m,'shutdown',side_effect=RuntimeError('still running')),patch.object(self.m,'activate') as activate:
            with self.assertRaisesRegex(RuntimeError,'still running'): self.m.rollback(record)
            activate.assert_not_called()
    def test_successful_transaction_switches_active_manifest(self):
        report=self.root/'success';report.mkdir()
        candidate={'Id':'b'*64,'Config':{'Cmd':['untrusted-inspect-cmd']},'State':{'ConmonPid':123}}
        old={'Config':{'Hostname':'oldhost','Env':[]}}
        def sql(db,q):
            paths={'SHOW config_file':'/etc/postgresql/postgresql.conf',
                   'SHOW hba_file':'/etc/postgresql/pg_hba.conf',
                   'SHOW ident_file':'/etc/postgresql/pg_ident.conf'}
            return paths.get(q,'')
        original_read=Path.read_text
        def read(path,*args,**kwargs):
            if str(path)=='/proc/123/cgroup': return '/system.slice/'+self.m.r.UNIT
            return original_read(path,*args,**kwargs)
        with patch.object(self.m,'guard',return_value=old),patch.object(self.m.r,'run'),patch.object(self.m,'shutdown'),patch.object(self.m.r,'pod'),patch.object(self.m.r,'inspect',return_value=candidate),patch.object(self.m,'verify_candidate'),patch.object(self.m.r,'healthy'),patch.object(self.m.r,'sql',side_effect=sql),patch.object(Path,'read_text',read):
            self.m.deploy('c'*40,Path('/release'),report)
        active=json.loads(self.m.ACTIVE.read_text())
        self.assertEqual(active['id'],'b'*64)
        self.assertEqual(active['cmd'][-1],'config_file=/etc/postgresql/postgresql.conf')
        self.assertEqual(active['mounts']['/etc/postgresql'],'/release')
        self.assertFalse(self.m.PENDING.exists())
        self.assertFalse((self.m.r.SERVICE/'down').exists())
        self.assertEqual(json.loads((report/'transaction.json').read_text())['phase'],'completed')
    def test_deploy_failure_records_rollback(self):
        report=self.root/'report';report.mkdir()
        with patch.object(self.m,'guard',return_value={}),patch.object(self.m.r,'run'),patch.object(self.m,'shutdown',side_effect=RuntimeError('injected')),patch.object(self.m,'rollback') as rollback:
            with self.assertRaisesRegex(RuntimeError,'injected'): self.m.deploy('c'*40,Path('/release'),report)
        rollback.assert_called_once()
        self.assertFalse(self.m.PENDING.exists())
        self.assertEqual(json.loads((report/'transaction.json').read_text())['phase'],'rolled_back')
    def test_rollback_failure_retains_pending(self):
        report=self.root/'report';report.mkdir()
        with patch.object(self.m,'guard',return_value={}),patch.object(self.m.r,'run'),patch.object(self.m,'shutdown',side_effect=RuntimeError('injected')),patch.object(self.m,'rollback',side_effect=RuntimeError('cannot stop')):
            with self.assertRaises(RuntimeError): self.m.deploy('c'*40,Path('/release'),report)
        self.assertEqual(json.loads(self.m.PENDING.read_text())['phase'],'rollback_failed')

if __name__=='__main__': unittest.main()
