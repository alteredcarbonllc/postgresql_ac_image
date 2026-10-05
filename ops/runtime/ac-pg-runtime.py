#!/usr/bin/python3
"""First-stage adoption of an existing PostgreSQL container. Never initdb/rm/create."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

NAME='postgresql_container_openmailserver.net'
IMAGE='61ab21d51c403c7f9c5f58720ee5ef9c7893b1cf4cc4f3eadd2c5f1054081fa3'
CID='21161ce062173685d98cf80d4b8b20d071186281d366163f890a14691114a1ec'
POD='/usr/local/bin/podman'
BIN='/usr/lib/postgresql/16/bin'
STATE=Path('/var/lib/ac-postgresql')
SERVICE=Path('/etc/ac/runit/postgresql')
MARKER=Path('/etc/ac/managed/postgresql')
UNIT='ac-postgresql-runit.service'
LIB=Path('/usr/local/libexec/ac-postgresql')
ENV={'HOME':'/root','USER':'root','LOGNAME':'root','PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8'}
MOUNTS={
 '/var/lib/postgresql/data':'/root/podman_network/postgresql1/var/lib/postgresql/data',
 '/var/log':'/var/volumes/log/postgresql_container_openmailserver.net/var/log',
}

def run(args,check=True,timeout=180):
    return subprocess.run(args,check=check,env=ENV,cwd='/',text=True,
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
def pod(*args,**kw): return run([POD,'--remote=false',*args],**kw)
def inspect(): return json.loads(pod('inspect',NAME).stdout)[0]
def require(ok,msg):
    if not ok: raise RuntimeError(msg)
def verify(c,managed=False):
    require(c['Id']==CID and c['Image'].removeprefix('sha256:')==IMAGE,'Unexpected container/image')
    require(c['Config']['User']=='postgres','Unexpected user')
    require(c['Config']['Cmd']==[BIN+'/postgres','-D','/var/lib/postgresql/data'],'Unexpected command')
    require(c['Config'].get('Entrypoint') in (None,[]),'Unexpected entrypoint')
    require(len(c['Mounts'])==2,'Unexpected extra mount')
    require({m['Destination'].rstrip('/'):m['Source'].rstrip('/') for m in c['Mounts']}==MOUNTS,'Unexpected mounts')
    require(all(m['Type']=='bind' and m['RW'] for m in c['Mounts']),'Unexpected mount mode')
    require(not c['HostConfig'].get('PortBindings'),'Published PostgreSQL port')
    # Podman may clear runtime addresses while a container is stopped.
    if c['State']['Running']:
        nets=c['NetworkSettings']['Networks']
        require(set(nets)=={'ac_network'},'Unexpected network')
        n=nets['ac_network']
        require((n['IPAddress'],n['GlobalIPv6Address'],n['MacAddress'].lower())==
                ('10.89.1.219','fd00:10:89:1::219','ee:86:c4:0f:1c:e4'),'Unexpected network address')
    policy=c['HostConfig']['RestartPolicy']['Name']
    require(policy in (('no','never','') if managed else ('always',)),'Unexpected restart policy')

def sql(db,query):
    return pod('exec','--user','postgres',NAME,BIN+'/psql','-X','-v','ON_ERROR_STOP=1',
               '-At','-d',db,'-c',query,timeout=20).stdout.strip()
def healthy(managed=True):
    started=None
    for attempt in range(30):
        c=inspect();verify(c,managed)
        if c['State']['Running']:
            try:
                require(sql('postgres','SHOW data_directory')=='/var/lib/postgresql/data','Wrong PGDATA')
                for db in ('postgres','ejabberd','mail','wp_carbonblog'):
                    require(sql(db,'SELECT 1')=='1','SQL health check failed')
                current=c['State']['StartedAt']
                if current==started: return
                started=current
            except subprocess.SubprocessError:
                started=None
        time.sleep(2)
    raise RuntimeError('Database health check failed')

def fast_stop():
    c=inspect()
    require(c['Id']==CID,'Refusing to signal unexpected container')
    require(c['HostConfig']['RestartPolicy']['Name'] in ('no','never',''),'Restart policy must be disabled')
    if c['State']['Running']:
        pod('kill','--signal','SIGINT',CID)
    for _ in range(120):
        c=inspect()
        require(c['Id']==CID,'Container changed while stopping')
        if not c['State']['Running']:
            require(c['State'].get('ExitCode')==0,'PostgreSQL did not exit cleanly')
            return
        time.sleep(1)
    raise RuntimeError('Fast shutdown timed out; NO SIGKILL was sent')

def atomic(path,data,mode=0o600):
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def script_status():
    statuses=[]
    for short in ('start','stop'):
        current=Path('/usr/bin/'+short+'_ac_containers.sh').read_bytes()
        before=(LIB/'legacy'/(short+'.before')).read_bytes()
        after=(LIB/'legacy'/(short+'.after')).read_bytes()
        require(current in (before,after),'Legacy '+short+' script differs from reviewed source')
        statuses.append(current==after)
    return statuses

def safe_old_unit():
    # Never stop this service: it still contains mail/ejabberd conmon processes.
    status=run(['/usr/bin/systemctl','show','ac_containers.service','-p','ActiveState','-p','SubState']).stdout
    require('ActiveState=active' in status and 'SubState=exited' in status,'Legacy service not idle active/exited')
    processes=run(['/usr/bin/pgrep','-f',r'^(/bin/bash|/usr/bin/bash) /usr/bin/(start|stop)_ac_containers.sh( |$)'],check=False)
    require(processes.returncode==1,'Legacy start/stop script is running, or process check failed')

def check():
    require(not MARKER.exists(),'Already adopted; use ac-pgctl check')
    verify(inspect(),False)
    require((SERVICE/'down').is_file(),'Supervisor must be installed down')
    safe_old_unit();script_status();healthy(False)
    require(sql('postgres','SHOW server_version_num')=='160006','Unexpected PostgreSQL version')
    print('CHECK_OK: same container/image, four databases; no changes made')

def migrate():
    if MARKER.exists():
        healthy();print('MIGRATE_OK: already adopted');return
    check()
    journal=Path(tempfile.mkdtemp(prefix='migration.',dir=STATE))
    before=inspect()
    (journal/'inspect.json').write_text(json.dumps(before,indent=2))
    for short in ('start','stop'):
        (journal/(short+'.saved')).write_bytes(Path('/usr/bin/'+short+'_ac_containers.sh').read_bytes())
    timer_on=run(['/usr/bin/systemctl','is-active','--quiet','ac_containers.timer'],check=False).returncode==0
    unit_enabled=run(['/usr/bin/systemctl','is-enabled','--quiet',UNIT],check=False).returncode==0
    record={'timer_was_active':timer_on,'unit_was_enabled':unit_enabled,'phase':'prepared'}
    def phase(value):
        record['phase']=value
        atomic(journal/'record.json',(json.dumps(record,indent=2)+'\n').encode())
        print('MIGRATION_PHASE: '+value,flush=True)
    phase('prepared')
    changed=False
    try:
        run(['/usr/bin/systemctl','stop','ac_containers.timer'])
        safe_old_unit()
        changed=True
        for short in ('start','stop'):
            atomic(Path('/usr/bin/'+short+'_ac_containers.sh'),(LIB/'legacy'/(short+'.after')).read_bytes(),0o755)
        atomic(MARKER,(str(journal)+'\n').encode())
        pod('update','--restart=no',CID)
        phase('legacy_excluded')
        fast_stop()
        phase('stopped_cleanly')
        run(['/usr/bin/systemctl','enable','--now',UNIT])
        (SERVICE/'down').unlink(missing_ok=True)
        run(['/usr/bin/sv','-w','140','up',str(SERVICE)],timeout=150)
        healthy()
        conmon=inspect()['State'].get('ConmonPid')
        require(conmon,'Missing conmon PID; cannot verify supervisor isolation')
        cg=Path(f'/proc/{conmon}/cgroup').read_text()
        require('ac_containers.service' not in cg and UNIT in cg,'Conmon is outside new supervisor cgroup')
        phase('healthy')
        if timer_on: run(['/usr/bin/systemctl','start','ac_containers.timer'])
        phase('completed')
        print('MIGRATE_OK: original container and data retained; '+str(journal))
    except Exception:
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,signal.SIG_IGN)
        phase('rollback_requested')
        try:
            if changed:
                (SERVICE/'down').touch()
                # sv may fail if supervisor was not started yet. Confirm with inspect below.
                run(['/usr/bin/sv','-w','140','down',str(SERVICE)],check=False,timeout=150)
                fast_stop()
                run(['/usr/bin/systemctl','stop',UNIT],timeout=180)
                if not unit_enabled: run(['/usr/bin/systemctl','disable',UNIT])
                for short in ('start','stop'):
                    atomic(Path('/usr/bin/'+short+'_ac_containers.sh'),(journal/(short+'.saved')).read_bytes(),0o755)
                MARKER.unlink(missing_ok=True)
                pod('update','--restart=always',CID)
                pod('start',CID)
                healthy(False)
            if timer_on: run(['/usr/bin/systemctl','start','ac_containers.timer'])
            phase('rolled_back');print('ROLLBACK_OK',flush=True)
        except Exception as error:
            phase('rollback_failed')
            print('ROLLBACK_FAILED: '+str(error)+'; journal='+str(journal),file=sys.stderr)
        raise

def main():
    require(os.geteuid()==0,'Run as root')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['check-legacy','migrate','check','start','stop','restart','reload','status','run','signal-stop'])
    action=parser.parse_args().action
    os.umask(0o077)
    # control/t cannot take the operation lock: sv down is waiting for it.
    if action=='signal-stop':
        try: fast_stop()
        except Exception as e: print('STOP_FAILED: '+str(e),file=sys.stderr)
        return # suppress default runsv SIGTERM even if shutdown times out
    if action=='run':
        require(MARKER.is_file(),'Not adopted')
        verify(inspect(),True)
        require(not inspect()['State']['Running'],'Container already running outside supervisor')
        os.execve(POD,[POD,'--remote=false','start','--attach','--sig-proxy=false',CID],ENV)
    with (STATE/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if action=='check-legacy': check()
        elif action=='migrate': migrate()
        elif action=='status':
            print(run(['/usr/bin/sv','status',str(SERVICE)],check=False).stdout,end='')
            c=inspect();print(json.dumps({'id':c['Id'],'running':c['State']['Running'],'image':c['Image']}))
        else:
            require(MARKER.is_file(),'Not adopted')
            verify(inspect(),True)
            if action in ('stop','restart'):
                (SERVICE/'down').touch()
                run(['/usr/bin/sv','-w','140','down',str(SERVICE)],timeout=150)
                require(not inspect()['State']['Running'],'Container still running')
            if action in ('start','restart'):
                (SERVICE/'down').unlink(missing_ok=True)
                run(['/usr/bin/sv','-w','140','up',str(SERVICE)],timeout=150)
                healthy()
            if action=='check': healthy();print('HEALTH_OK: four databases')
            if action=='reload':
                require(sql('postgres','SELECT count(*) FROM pg_file_settings WHERE error IS NOT NULL')=='0','Configuration parse errors')
                require(sql('postgres','SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL')=='0','HBA parse errors')
                require(sql('postgres','SELECT pg_reload_conf()')=='t','Reload signal failed')
                healthy();print('RELOAD_SIGNAL_OK: inspect logs and pending_restart before assuming all settings applied')

def interrupted(signum,frame): raise RuntimeError('Interrupted by signal '+str(signum))
if __name__=='__main__':
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,interrupted)
    try: main()
    except Exception as e:
        print('PG_RUNTIME_ERROR: '+str(e),file=sys.stderr);sys.exit(1)
