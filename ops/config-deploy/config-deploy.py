#!/usr/bin/python3
"""Manual stage-2 config deployment on current VPS; fixed PostgreSQL image."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

spec=importlib.util.spec_from_file_location('runtime',Path(__file__).with_name('ac-pg-runtime.py'))
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
FILES=('postgresql.conf','pg_hba.conf','pg_ident.conf')
ACTIVE=r.STATE/'active.json'
PENDING=r.STATE/'pending-config-deploy.json'

def save(path,value): r.atomic(path,(json.dumps(value,indent=2)+'\n').encode())
def byid(cid): return json.loads(r.pod('inspect',cid).stdout)[0]
def activate(value):
    save(ACTIVE,value)
    r.load_active()
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def prepare(repo):
    repo=Path(repo).resolve()
    r.require(repo.is_dir(),'Repository missing')
    # Only committed blobs are imported; no git hooks/checkouts are executed.
    def git(*args): return r.run(['/usr/bin/git','-C',str(repo),*args]).stdout
    r.require(not git('status','--porcelain'),'Config checkout is dirty')
    commit=git('rev-parse','HEAD').strip()
    r.require(re.fullmatch('[0-9a-f]{40}',commit),'Unexpected commit ID')
    dest=r.STATE/'config-releases'/commit
    dest.parent.mkdir(mode=0o700,exist_ok=True)
    blobs={}
    for name in FILES:
        tree=git('ls-tree',commit,'--','postgresql/'+name)
        r.require(tree.startswith('100644 blob '),'Config must be a regular non-executable blob: '+name)
        blob=r.run(['/usr/bin/git','-C',str(repo),'show',commit+':postgresql/'+name]).stdout.encode()
        r.require(len(blob)<1024*1024,'Config file too large')
        blobs[name]=blob
    if dest.exists():
        r.require(not dest.is_symlink() and {p.name for p in dest.iterdir()}==set(FILES),'Unexpected release contents')
        for name,data in blobs.items(): r.require((dest/name).read_bytes()==data,'Release changed')
    else:
        tmp=Path(tempfile.mkdtemp(prefix='.prepare.',dir=dest.parent))
        os.chmod(tmp,0o755)
        for name,data in blobs.items(): r.atomic(tmp/name,data,0o444)
        os.rename(tmp,dest)
    return commit,dest

def config_sql(test=False):
    overrides="""AND NOT (name IN ('data_directory','listen_addresses','unix_socket_directories')
                   AND error='setting could not be applied')""" if test else ''
    return """DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_file_settings WHERE error IS NOT NULL %s)
        OR EXISTS (SELECT FROM pg_hba_file_rules WHERE error IS NOT NULL)
        OR EXISTS (SELECT FROM pg_ident_file_mappings WHERE error IS NOT NULL)
      THEN RAISE EXCEPTION 'Configuration error'; END IF;
    END $$;""" % overrides

def test_release(dest,report):
    name='ac-pg-config-check-'+report.name.replace('.','-')
    command='''set -eu
bin=/usr/lib/postgresql/16/bin
if [ ! -f /tmp/ac-config-data/PG_VERSION ]; then
 "$bin/initdb" -D /tmp/ac-config-data --auth-local=trust --auth-host=reject --encoding=UTF8 --locale=C.UTF-8
fi
exec "$bin/postgres" -D /tmp/ac-config-data -c config_file=/etc/postgresql/postgresql.conf -c data_directory=/tmp/ac-config-data -c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp
'''
    cid=r.pod('create','--pull=never','--name',name,'--network=none','--restart=no',
              '--user=postgres','--stop-signal=SIGINT','--stop-timeout=120',
              '-v',str(dest)+':/etc/postgresql:ro','--entrypoint=/bin/sh',r.IMAGE,'-ec',command).stdout.strip()
    (report/'test-id.txt').write_text(cid+'\n')
    success=False
    try:
        r.pod('start',cid)
        for attempt in range(30):
            r.require(byid(cid)['State']['Running'],'Test exited; see report')
            p=r.pod('exec',cid,r.BIN+'/pg_isready','-h','/tmp','-U','postgres','-d','postgres',check=False)
            if p.returncode==0: break
            time.sleep(1)
        else: raise RuntimeError('Test startup timed out')
        def sql(q):
            return r.pod('exec',cid,r.BIN+'/psql','-X','-v','ON_ERROR_STOP=1','-At',
                         '-h','/tmp','-U','postgres','-d','postgres','-c',q).stdout.strip()
        effective={'data_directory':'/tmp/ac-config-data','listen_addresses':'127.0.0.1',
                   'unix_socket_directories':'/tmp','config_file':'/etc/postgresql/postgresql.conf',
                   'hba_file':'/etc/postgresql/pg_hba.conf','ident_file':'/etc/postgresql/pg_ident.conf'}
        for key,value in effective.items(): r.require(sql('SHOW '+key)==value,'Unexpected test '+key)
        # Diagnostic details are retained before raising on an error.
        (report/'test-settings.txt').write_text(sql('SELECT sourcefile,sourceline,name,applied,error FROM pg_file_settings'))
        sql(config_sql(True))
        success=True
    finally:
        c=byid(cid)
        if c['State']['Running']:
            r.pod('kill','--signal=SIGINT',cid)
            for _ in range(120):
                if not byid(cid)['State']['Running']: break
                time.sleep(1)
        c=byid(cid)
        (report/'test-container.log').write_text(r.pod('logs',cid,check=False).stdout)
        r.require(not c['State']['Running'],'Test did not stop; no SIGKILL sent: '+cid)
        r.require(c['State']['ExitCode']==0,'Test exit was not clean: '+cid)
        if success: r.pod('rm',cid)
        else: print('TEST_RETAINED: '+name,flush=True)
    print('CONFIG_TEST_OK',flush=True)

def shutdown(cid, allow_failed=False):
    c=byid(cid)
    r.require(c['HostConfig']['RestartPolicy']['Name'] in ('no','never',''),'Competing restart policy')
    if c['State']['Running']: r.pod('kill','--signal=SIGINT',cid)
    for _ in range(120):
        c=byid(cid)
        if not c['State']['Running']:
            # Newly created/unstarted containers have no PostgreSQL exit to check.
            r.require(allow_failed or c['State']['Status']=='created' or c['State']['ExitCode']==0,'Unclean exit; manual inspection needed')
            return
        time.sleep(1)
    raise RuntimeError('Shutdown timeout; no SIGKILL sent')

def guard():
    r.load_active()
    r.require(r.MARKER.is_file(),'Legacy exclusion marker absent')
    r.require(not (r.SERVICE/'down').exists(),'Service is down')
    r.require(all(r.script_status()),'Legacy launcher guards differ')
    r.healthy()
    c=r.inspect()
    h=c['HostConfig']
    r.require(c['Config']['WorkingDir']=='/','Working directory changed')
    r.require(not h.get('Privileged') and not h.get('ReadonlyRootfs'),'Unexpected privilege/rootfs settings')
    r.require(h.get('UsernsMode','')=='' and not h.get('SecurityOpt') and not h.get('CapAdd') and not h.get('CapDrop'),'Security settings changed')
    r.require(h['ShmSize']==65536000 and h['Memory']==0 and h['PidsLimit']==0,'Resource settings changed')
    r.require(h['Ulimits']==[{'Name':'RLIMIT_NPROC','Soft':4194304,'Hard':4194304}],'Ulimits changed')
    r.require(h['LogConfig']['Type']=='k8s-file','Log driver changed')
    r.require(r.sql('postgres','SHOW server_version_num')=='160006','Image/version changed')
    r.require(r.sql('postgres',"SELECT count(*) FROM pg_file_settings WHERE sourcefile LIKE '%/postgresql.auto.conf'")=='0','ALTER SYSTEM overrides present; review first')
    return c

def create_args(old,dest,name):
    args=['create','--pull=never','--name',name,'--hostname',old['Config']['Hostname'],
          '--workdir=/','--user=postgres','--restart=no','--stop-signal=SIGINT','--stop-timeout=120',
          '--network=ac_network','--ip=10.89.1.219','--ip6=fd00:10:89:1::219',
          '--mac-address=ee:86:c4:0f:1c:e4','--shm-size=65536000b','--pids-limit=0',
          '--ulimit=nproc=4194304:4194304','--log-driver=k8s-file']
    # Retain the old environment exactly; image remains identical.
    for item in old['Config'].get('Env') or []: args+=['--env',item]
    for target,source in r.MOUNTS.items():
        if target!='/etc/postgresql': args+=['-v',source+':'+target+':Z']
    args+=['-v',str(dest)+':/etc/postgresql:ro',r.IMAGE,r.BIN+'/postgres',
           '-D','/var/lib/postgresql/data','-c','config_file=/etc/postgresql/postgresql.conf']
    return args

def verify_candidate(c,old):
    r.verify(c,True)
    for key in ('Hostname','WorkingDir','User','Env'):
        r.require(c['Config'].get(key)==old['Config'].get(key),'Candidate differs: '+key)
    for key in ('Privileged','ReadonlyRootfs','UsernsMode','SecurityOpt','CapAdd','CapDrop','ShmSize','Memory','PidsLimit','Ulimits'):
        r.require(c['HostConfig'].get(key)==old['HostConfig'].get(key),'Candidate differs: '+key)
    r.require(c['Config']['StopSignal'] in ('SIGINT','2',2),'Candidate stop signal mismatch')
    r.require(c['Config']['StopTimeout']==120,'Candidate stop timeout mismatch')

def deploy(commit,dest,report):
    old=guard()
    previous=json.loads(ACTIVE.read_text())
    r.require(previous.get('config_commit')!=commit,'Already deployed; use check/status')
    record={'phase':'prepared','old':previous,'old_inspect':old,'commit':commit,
            'backup_name':r.NAME+'-rollback-'+report.name,'candidate':None,'report':str(report)}
    def phase(value):
        record['phase']=value;save(report/'transaction.json',record);save(PENDING,record)
        print('DEPLOY_PHASE: '+value,flush=True)
    phase('prepared')
    try:
        (r.SERVICE/'down').touch()
        r.run(['/usr/bin/sv','-w','140','down',str(r.SERVICE)],timeout=150)
        shutdown(previous['id'])
        phase('old_stopped')
        r.pod('rename',previous['id'],record['backup_name'])
        phase('old_renamed')
        # Unique expected name lets rollback find a candidate after a CLI interruption.
        r.pod(*create_args(old,dest,r.NAME))
        candidate=r.inspect()
        record['candidate']=candidate['Id']
        value={'id':candidate['Id'],'image':r.IMAGE,'cmd':candidate['Config']['Cmd'],
               'mounts':{**previous['mounts'],'/etc/postgresql':str(dest)},'config_commit':commit}
        # Do not accept a command from inspect as its own validation reference.
        value['cmd']=[r.BIN+'/postgres','-D','/var/lib/postgresql/data','-c','config_file=/etc/postgresql/postgresql.conf']
        activate(value)
        verify_candidate(candidate,old)
        phase('candidate_created')
        (r.SERVICE/'down').unlink()
        r.run(['/usr/bin/sv','-w','140','up',str(r.SERVICE)],timeout=150)
        r.healthy()
        r.sql('postgres',config_sql(False))
        for key in ('config_file','hba_file','ident_file'):
            name={'config_file':'postgresql.conf','hba_file':'pg_hba.conf','ident_file':'pg_ident.conf'}[key]
            r.require(r.sql('postgres','SHOW '+key)=='/etc/postgresql/'+name,'Wrong effective '+key)
        conmon=r.inspect()['State']['ConmonPid']
        r.require(r.UNIT in Path(f'/proc/{conmon}/cgroup').read_text(),'Wrong supervisor cgroup')
        phase('completed')
        PENDING.unlink()
        print('CONFIG_DEPLOY_OK: '+commit+'; rollback container='+record['backup_name'])
    except BaseException:
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,signal.SIG_IGN)
        try:
            phase('rollback_requested')
            rollback(record)
            phase('rolled_back');PENDING.unlink()
            print('ROLLBACK_OK: original container running',flush=True)
        except BaseException as e:
            phase('rollback_failed')
            print('ROLLBACK_FAILED: '+str(e)+'; '+str(report),flush=True)
        raise

def rollback(record):
    (r.SERVICE/'down').touch()
    r.run(['/usr/bin/sv','-w','140','down',str(r.SERVICE)],check=False,timeout=150)
    oldid=record['old']['id']
    current=r.pod('container','exists',r.NAME,check=False)
    r.require(current.returncode in (0,1),'Cannot inspect current container')
    if current.returncode==0:
        c=r.inspect()
        if c['Id']!=oldid:
            r.require(c['Image'].removeprefix('sha256:')==r.IMAGE,'Unexpected candidate image')
            expected=record.get('candidate')
            r.require(expected is None or expected==c['Id'],'Unexpected candidate ID')
            shutdown(c['Id'],allow_failed=True)
            r.pod('rename',c['Id'],r.NAME+'-failed-'+Path(record['report']).name)
        else: shutdown(oldid)
    old=byid(oldid)
    r.require(not old['State']['Running'],'Old container unexpectedly running')
    if old['Name'].lstrip('/')!=r.NAME: r.pod('rename',oldid,r.NAME)
    activate(record['old'])
    (r.SERVICE/'down').unlink(missing_ok=True)
    r.run(['/usr/bin/sv','-w','140','up',str(r.SERVICE)],timeout=150)
    r.healthy()

def main():
    r.require(os.geteuid()==0,'Run as root')
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('check','deploy'))
    p.add_argument('repo',help='Root-owned local postgresql_config checkout')
    a=p.parse_args()
    with (r.STATE/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        r.require(not PENDING.exists(),'Unfinished deployment: inspect '+str(PENDING)+'; do not retry blindly')
        guard()
        commit,dest=prepare(a.repo)
        report=Path(tempfile.mkdtemp(prefix='config-deploy.',dir=r.STATE))
        save(report/'release.json',{'commit':commit,'files':{name:digest(dest/name) for name in FILES}})
        print('REPORT='+str(report),flush=True)
        test_release(dest,report)
        if a.action=='deploy': deploy(commit,dest,report)
        else: print('CONFIG_PREFLIGHT_OK: '+commit+'; production unchanged')

if __name__=='__main__':
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,r.interrupted)
    try: main()
    except BaseException as e:
        print('CONFIG_DEPLOY_ERROR: '+str(e)+'; '+str(getattr(e,'stderr',''))[-3000:],flush=True)
        raise SystemExit(1)
