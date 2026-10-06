import copy
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import unittest

BASE=Path(__file__).resolve().parents[1]
def load(name,path):
    loader=importlib.machinery.SourceFileLoader(name,str(path))
    spec=importlib.util.spec_from_loader(name,loader)
    m=importlib.util.module_from_spec(spec);loader.exec_module(m)
    return m
imp=load('pg_import',BASE/'ac-postgresql-import')
test=load('pg_test',BASE/'test-image.py')
REV='a'*40

def archive(tag=None,label=REV,special=False,duplicate=False):
    buff=io.BytesIO()
    manifest=[{'Config':'config.json','RepoTags':[tag or 'localhost/postgresql-ac:'+REV],'Layers':['layer.tar']}]
    config={'config':{'Labels':{'org.opencontainers.image.revision':label}}}
    with tarfile.open(fileobj=buff,mode='w') as tar:
        for name,data in [('manifest.json',json.dumps(manifest).encode()),('config.json',json.dumps(config).encode()),('layer.tar',b'layer')]:
            member=tarfile.TarInfo(name);member.size=len(data)
            if special and name=='layer.tar': member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd';member.size=0
            tar.addfile(member,io.BytesIO(data) if member.isfile() else None)
        if duplicate:
            member=tarfile.TarInfo('config.json');member.size=0;tar.addfile(member,io.BytesIO())
    buff.seek(0)
    return tarfile.open(fileobj=buff,mode='r:')

class Import(unittest.TestCase):
    def test_valid_receipt(self):
        with archive() as a:
            ref,ident,selected=imp.manifest_info(a,REV)
        self.assertEqual(ref,'localhost/postgresql-ac:'+REV)
        self.assertEqual(len(ident),71)
        self.assertEqual(len(selected),3)
    def test_wrong_tag(self):
        with archive(tag='localhost/php-carbonblog:'+REV) as a:
            with self.assertRaises(ValueError): imp.manifest_info(a,REV)
    def test_wrong_revision(self):
        with archive(label='b'*40) as a:
            with self.assertRaises(ValueError): imp.manifest_info(a,REV)
    def test_link_layer_refused(self):
        with archive(special=True) as a:
            with self.assertRaises(ValueError): imp.manifest_info(a,REV)
    def test_duplicate_refused(self):
        with archive(duplicate=True) as a:
            with self.assertRaises(ValueError): imp.manifest_info(a,REV)

class Image(unittest.TestCase):
    def data(self):
        return {'Architecture':'amd64','Os':'linux','Config':{'User':'postgres','Entrypoint':None,
          'Cmd':[test.BIN+'/postgres','-D','/var/lib/postgresql/data'],
          'Labels':{'org.opencontainers.image.revision':REV},'StopSignal':'SIGINT'}}
    def test_valid(self): test.validate_image(self.data(),REV)
    def test_root_refused(self):
        d=self.data();d['Config']['User']='root'
        with self.assertRaises(RuntimeError): test.validate_image(d,REV)
    def test_automatic_initialization_refused(self):
        d=self.data();d['Config']['Entrypoint']=['entrypoint.sh']
        with self.assertRaises(RuntimeError): test.validate_image(d,REV)
    def test_anonymous_volumes_refused(self):
        d=self.data();d['Config']['Volumes']={'/var/lib/postgresql/data':{}}
        with self.assertRaises(RuntimeError): test.validate_image(d,REV)
    def test_wrong_revision_refused(self):
        with self.assertRaises(RuntimeError): test.validate_image(self.data(),'b'*40)

if __name__=='__main__': unittest.main()
