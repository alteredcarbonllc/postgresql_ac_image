import copy
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
import test_config_deploy as base_tests

# Reuse only fixture, not inherited test methods.
import unittest
class ImageTests(unittest.TestCase):
    def setUp(self):
        base_tests.Tests.setUp(self)
        # Temporary fixtures belong to the test runner on laptops, not root.
        original=Path.stat
        import os
        def stat_as_root(path,*a,**kw):
            st=original(path,*a,**kw)
            fields=list(st);fields[4]=0
            return os.stat_result(fields)
        self.owner_patch=patch.object(Path,'stat',stat_as_root)
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)
        self.lstat_patch=patch.object(
            Path, 'lstat',
            lambda path: stat_as_root(path, follow_symlinks=False),
        )
        self.lstat_patch.start()
        self.addCleanup(self.lstat_patch.stop)

    def receipt(self):
        folder=self.root/'imports';folder.mkdir(exist_ok=True)
        value={'revision':self.m.APPROVED_REVISION,'image':'localhost/postgresql-ac:'+self.m.APPROVED_REVISION,'image_id':'sha256:'+self.m.CANDIDATE_IMAGE}
        p=folder/(self.m.APPROVED_REVISION+'.json')
        p.write_text(json.dumps(value));p.chmod(0o600)
        image={'Id':value['image_id'],'Architecture':'amd64','Os':'linux',
               'Config':{'User':'postgres','Entrypoint':None,'Volumes':None,
                         'Cmd':[self.m.r.BIN+'/postgres','-D','/var/lib/postgresql/data'],
                         'StopSignal':'SIGINT','Labels':{'org.opencontainers.image.revision':self.m.APPROVED_REVISION}}}
        return p,value,image

    def test_approved_receipt_and_immutable_id(self):
        p,v,img=self.receipt()
        with patch.object(self.m.r,'pod',return_value=subprocess.CompletedProcess([],0,json.dumps([img]))):
            self.assertEqual(self.m.imported_image(self.m.APPROVED_REVISION),self.m.CANDIDATE_IMAGE)
            img['Id']='a'*64
        with patch.object(self.m.r,'pod',return_value=subprocess.CompletedProcess([],0,json.dumps([img]))):
            with self.assertRaisesRegex(RuntimeError,'tag/image ID'): self.m.imported_image(self.m.APPROVED_REVISION)

    def test_wrong_revision_receipt_and_symlink_refused(self):
        p,v,img=self.receipt()
        with self.assertRaisesRegex(RuntimeError,'not been approved'): self.m.imported_image('a'*40)
        v['image_id']='a'*64;p.write_text(json.dumps(v))
        with self.assertRaisesRegex(RuntimeError,'receipt image ID|Receipt image ID'): self.m.imported_image(self.m.APPROVED_REVISION)
        dest=p.with_suffix('.saved');p.rename(dest);p.symlink_to(dest)
        with self.assertRaisesRegex(RuntimeError,'symlink'): self.m.imported_image(self.m.APPROVED_REVISION)

    def test_image_metadata_refused(self):
        p,v,img=self.receipt()
        for key,value in [('User','root'),('Entrypoint',['initdb']),('Volumes',{'/data':{}}),('StopSignal','SIGTERM')]:
            changed=copy.deepcopy(img);changed['Config'][key]=value
            with self.subTest(key=key),patch.object(self.m.r,'pod',return_value=subprocess.CompletedProcess([],0,json.dumps([changed]))):
                with self.assertRaises(RuntimeError): self.m.imported_image(self.m.APPROVED_REVISION)

    def test_runtime_switches_both_directions_and_refuses_unknown(self):
        for image in (self.m.CANDIDATE_IMAGE,self.m.r.LEGACY_IMAGE):
            active={**self.old,'image':image};self.m.save(self.m.ACTIVE,active)
            self.m.r.load_active();self.assertEqual(self.m.r.IMAGE,image)
        self.m.save(self.m.ACTIVE,{**self.old,'image':'d'*64})
        with self.assertRaisesRegex(RuntimeError,'Unapproved'): self.m.r.load_active()

    def test_current_config_path_and_symlink(self):
        commit='c'*40;dest=self.root/'config-releases'/commit;dest.mkdir(parents=True)
        dest.chmod(0o755)
        for n in self.m.FILES:
            (dest/n).write_text('# test\n')
            (dest/n).chmod(0o444)
        active={**self.old,'config_commit':commit,'mounts':{**self.old['mounts'],'/etc/postgresql':str(dest)}}
        self.m.save(self.m.ACTIVE,active)
        self.assertEqual(self.m.current_release(),(commit,dest))
        (dest/'pg_ident.conf').chmod(0o666)
        with self.assertRaisesRegex(RuntimeError, 'root protected'):
            self.m.current_release()
        (dest/'pg_ident.conf').chmod(0o444)
        (dest/'pg_ident.conf').unlink();(dest/'pg_ident.conf').symlink_to('/etc/passwd')
        with self.assertRaises(RuntimeError): self.m.current_release()

    def test_candidate_rollback_uses_transaction_image_not_loaded_image(self):
        events=[]
        record={'old':self.old,'candidate':'b'*64,'target_image':self.m.CANDIDATE_IMAGE,'report':str(self.root/'report')}
        candidate={'Id':'b'*64,'Image':self.m.CANDIDATE_IMAGE}
        with patch.object(self.m.r,'pod',return_value=subprocess.CompletedProcess([],0)),patch.object(self.m.r,'inspect',return_value=candidate),patch.object(self.m,'shutdown',side_effect=lambda *a,**kw:events.append('stop')),patch.object(self.m,'byid',return_value={'Name':'backup','State':{'Running':False}}),patch.object(self.m.r,'run',side_effect=lambda *a,**kw:events.append(a[0][3] if len(a[0])>3 else 'run')),patch.object(self.m.r,'healthy'):
            self.m.rollback(record)
        self.assertEqual(self.m.r.IMAGE,self.old['image'])
        self.assertLess(events.index('stop'),events.index('up'))

    def test_image_deploy_same_config_and_post_start_failure_rolls_back(self):
        commit='c'*40
        self.old['config_commit']=commit;self.m.save(self.m.ACTIVE,self.old)
        report=self.root/'upgrade';report.mkdir()
        candidate={'Id':'b'*64,'Config':{'Cmd':[]}}
        old={'Config':{'Hostname':'old','Env':[]}}
        created=[]
        def pod(*args,**kwargs): created.append(args)
        with patch.object(self.m,'guard',return_value=old),patch.object(self.m.r,'run'),patch.object(self.m,'shutdown'),patch.object(self.m.r,'pod',side_effect=pod),patch.object(self.m.r,'inspect',return_value=candidate),patch.object(self.m,'verify_candidate'),patch.object(self.m.r,'healthy',side_effect=RuntimeError('after start')),patch.object(self.m,'rollback') as rollback:
            with self.assertRaisesRegex(RuntimeError,'after start'):
                self.m.deploy(commit,Path('/release'),report,self.m.CANDIDATE_IMAGE,self.m.APPROVED_REVISION)
        self.assertTrue(any(a[0]=='create' and self.m.CANDIDATE_IMAGE in a for a in created))
        self.assertEqual(rollback.call_args.args[0]['old']['image'],self.old['image'])
        self.assertEqual(rollback.call_args.args[0]['target_image'],self.m.CANDIDATE_IMAGE)
        self.assertFalse(self.m.PENDING.exists())

    def test_image_success_preserves_config_and_data(self):
        commit='c'*40
        self.old['config_commit']=commit;self.m.save(self.m.ACTIVE,self.old)
        report=self.root/'upgrade';report.mkdir()
        candidate={'Id':'b'*64,'Config':{'Cmd':[]},'State':{'ConmonPid':123}}
        old={'Config':{'Hostname':'old','Env':[]}}
        def sql(db,q):
            return {'SHOW config_file':'/etc/postgresql/postgresql.conf','SHOW hba_file':'/etc/postgresql/pg_hba.conf','SHOW ident_file':'/etc/postgresql/pg_ident.conf'}.get(q,'')
        original=Path.read_text
        def read(p,*a,**kw):
            return self.m.r.UNIT if str(p)=='/proc/123/cgroup' else original(p,*a,**kw)
        with patch.object(self.m,'guard',return_value=old),patch.object(self.m.r,'run'),patch.object(self.m,'shutdown'),patch.object(self.m.r,'pod'),patch.object(self.m.r,'inspect',return_value=candidate),patch.object(self.m,'verify_candidate'),patch.object(self.m.r,'healthy'),patch.object(self.m.r,'sql',side_effect=sql),patch.object(Path,'read_text',read):
            self.m.deploy(commit,Path('/release'),report,self.m.CANDIDATE_IMAGE,self.m.APPROVED_REVISION)
        value=json.loads(self.m.ACTIVE.read_text())
        self.assertEqual(value['image'],self.m.CANDIDATE_IMAGE)
        self.assertEqual(value['image_revision'],self.m.APPROVED_REVISION)
        self.assertEqual(value['config_commit'],commit)
        self.assertEqual(value['mounts']['/var/lib/postgresql/data'],'/data')
        self.assertFalse(self.m.PENDING.exists())
