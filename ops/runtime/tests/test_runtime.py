import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('pg',ROOT/'ac-pg-runtime.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

def fixture():
    return {'Id':p.CID,'Image':p.IMAGE,'Config':{'User':'postgres','Cmd':[p.BIN+'/postgres','-D','/var/lib/postgresql/data'],'Entrypoint':None},
      'Mounts':[{'Source':v,'Destination':k,'Type':'bind','RW':True} for k,v in p.MOUNTS.items()],
      'HostConfig':{'PortBindings':{},'RestartPolicy':{'Name':'always'}},
      'NetworkSettings':{'Networks':{'ac_network':{'IPAddress':'10.89.1.219','GlobalIPv6Address':'fd00:10:89:1::219','MacAddress':'ee:86:c4:0f:1c:e4'}}},
      'State':{'Running':True,'ExitCode':0,'StartedAt':'time1','ConmonPid':123}}

class Tests(unittest.TestCase):
    def test_accept_legacy_and_managed(self):
        c=fixture();p.verify(c)
        c['HostConfig']['RestartPolicy']['Name']='no';p.verify(c,True)
    def test_reject_drift(self):
        for field,value in [('Id','other'),('Image','bad')]:
            c=fixture();c[field]=value
            with self.assertRaises(RuntimeError):p.verify(c)
        c=fixture();c['Mounts'][0]['Source']='/tmp/wrong'
        with self.assertRaises(RuntimeError):p.verify(c)
        c=fixture();c['HostConfig']['PortBindings']={'5432/tcp':[{}]}
        with self.assertRaises(RuntimeError):p.verify(c)
        c=fixture();c['NetworkSettings']['Networks']['ac_network']['IPAddress']='10.89.1.99'
        with self.assertRaises(RuntimeError):p.verify(c)
    def test_stopped_container_has_no_runtime_address(self):
        c=fixture();c['State']['Running']=False;c['NetworkSettings']['Networks']={}
        c['HostConfig']['RestartPolicy']['Name']='no';p.verify(c,True)
    def test_stop_uses_int(self):
        c=fixture();c['HostConfig']['RestartPolicy']['Name']='no'
        stopped=copy.deepcopy(c);stopped['State']['Running']=False
        with patch.object(p,'inspect',side_effect=[c,stopped]),patch.object(p,'pod') as pod:
            p.fast_stop();pod.assert_called_once_with('kill','--signal','SIGINT',p.CID)
    def test_stop_timeout_never_kills(self):
        c=fixture();c['HostConfig']['RestartPolicy']['Name']='no'
        with patch.object(p,'inspect',return_value=c),patch.object(p,'pod') as pod,patch.object(p.time,'sleep'):
            with self.assertRaises(RuntimeError):p.fast_stop()
            self.assertEqual(pod.call_count,1)
    def test_stop_refuses_competing_restart(self):
        with patch.object(p,'inspect',return_value=fixture()),patch.object(p,'pod') as pod:
            with self.assertRaises(RuntimeError):p.fast_stop()
            pod.assert_not_called()
    def test_unclean_exit_rejected(self):
        c=fixture();c['HostConfig']['RestartPolicy']['Name']='no'
        c['State'].update(Running=False,ExitCode=137)
        with patch.object(p,'inspect',return_value=c):
            with self.assertRaises(RuntimeError):p.fast_stop()
    def test_atomic_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            f=Path(d)/'record';p.atomic(f,b'one');p.atomic(f,b'two')
            self.assertEqual(f.read_bytes(),b'two');self.assertEqual(f.stat().st_mode & 0o777,0o600)
    def test_legacy_guard_is_only_behavior_change(self):
        before=(ROOT/'legacy/start.before').read_text()
        after=(ROOT/'legacy/start.after').read_text()
        a=before.index('echo "Starting PostgreSQL container..."');b=before.index('echo "Starting Dovecot container..."')
        self.assertEqual(after[after.index('echo "Starting PostgreSQL container..."'):after.index('echo "Starting Dovecot container..."')].removesuffix('fi\n\n'),before[a:b])
        self.assertTrue(after.endswith(before[b:]))
        self.assertIn('/etc/ac/managed/postgresql',(ROOT/'legacy/stop.after').read_text())
    def test_migration_failure_restores_scripts_and_policy(self):
        with tempfile.TemporaryDirectory() as d:
            state=Path(d);svc=state/'svc';svc.mkdir();(svc/'down').touch()
            marker=state/'marker';lib=state/'lib/legacy';lib.mkdir(parents=True)
            for short in ['start','stop']:(lib/(short+'.after')).write_bytes(b'after')
            c=fixture();writes=[]
            orig_read=Path.read_bytes
            def read(path):
                if str(path).startswith('/usr/bin/'):
                    return b'before'
                return orig_read(path)
            orig_atomic=p.atomic
            def atomic(path,data,mode=0o600):
                if str(path).startswith('/usr/bin/'):
                    writes.append((str(path),data))
                else:orig_atomic(path,data,mode)
            calls=[]
            def run(args,**kw):
                calls.append(args)
                return subprocess.CompletedProcess(args,0,stdout='',stderr='')
            stopped_calls=0
            def stop():
                nonlocal stopped_calls
                stopped_calls+=1
                if stopped_calls==1:raise RuntimeError('injected stop failure')
            with patch.object(p,'STATE',state),patch.object(p,'SERVICE',svc),patch.object(p,'MARKER',marker),patch.object(p,'LIB',lib.parent),patch.object(p,'check'),patch.object(p,'inspect',return_value=c),patch.object(p,'safe_old_unit'),patch.object(p,'run',side_effect=run),patch.object(p,'pod') as pod,patch.object(p,'healthy'),patch.object(p,'fast_stop',side_effect=stop),patch.object(p,'atomic',side_effect=atomic),patch.object(Path,'read_bytes',read),patch.object(p.signal,'signal'):
                with self.assertRaisesRegex(RuntimeError,'injected'):p.migrate()
            self.assertFalse(marker.exists())
            self.assertEqual([data for _,data in writes],[b'after',b'after',b'before',b'before'])
            self.assertIn(unittest.mock.call('update','--restart=always',p.CID),pod.call_args_list)
            self.assertFalse(any('ac_containers.service' in cmd and 'stop' in cmd for cmd in calls))
            record=json.loads(next(state.glob('migration.*/record.json')).read_text())
            self.assertEqual(record['phase'],'rolled_back')

if __name__=='__main__':unittest.main()
