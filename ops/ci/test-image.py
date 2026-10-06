#!/usr/bin/python3
"""Isolated candidate test: init, read/write, dump/restore, shutdown/restart."""
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid

POD='/usr/local/bin/podman'
BIN='/usr/lib/postgresql/16/bin'
def run(*args,check=True,timeout=180):
    return subprocess.run([POD,'--remote=false',*args],check=check,text=True,
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
def inspect(ref): return json.loads(run('inspect',ref).stdout)[0]
def require(value,message):
    if not value: raise RuntimeError(message)

def validate_image(data,revision):
    cfg=data['Config']
    require(data.get('Architecture')=='amd64','Unexpected image architecture')
    require(data.get('Os')=='linux','Unexpected image OS')
    require(cfg.get('User')=='postgres','Unexpected image user')
    require(cfg.get('Entrypoint') in (None,[]),'Unexpected entrypoint')
    require(cfg.get('Cmd')==[BIN+'/postgres','-D','/var/lib/postgresql/data'],'Unexpected CMD')
    require(not cfg.get('Volumes'),'Image must not create anonymous volumes')
    labels=cfg.get('Labels') or data.get('Labels') or {}
    require(labels.get('org.opencontainers.image.revision')==revision,'Revision label mismatch')
    require(cfg.get('StopSignal') in ('SIGINT','2',2),'Unexpected stop signal')

def main():
    require(len(sys.argv)==3,'Usage: test-image.py IMAGE FULL_SHA')
    image,revision=sys.argv[1:]
    require(re.fullmatch('[0-9a-f]{40}',revision),'Invalid revision')
    require(image=='localhost/postgresql-ac:'+revision,'Unexpected image tag')
    validate_image(json.loads(run('image','inspect',image).stdout)[0],revision)
    name='ac-pg-image-check-'+uuid.uuid4().hex[:16]
    script='''set -eu
bin=/usr/lib/postgresql/16/bin
test "$(id -u)" = 101
test "$(id -g)" = 103
test ! -e /var/lib/postgresql/16/main/PG_VERSION
if [ ! -f /tmp/ac-ci-data/PG_VERSION ]; then
 "$bin/initdb" -D /tmp/ac-ci-data --auth-local=trust --auth-host=reject --locale=C.UTF-8 --encoding=UTF8
fi
exec "$bin/postgres" -D /tmp/ac-ci-data -k /tmp -c listen_addresses=""
'''
    run('create','--pull=never','--name',name,'--network=none','--restart=no',
        '--stop-signal=SIGINT','--stop-timeout=120','--entrypoint=/bin/sh',image,'-ec',script)
    success=False
    def sql(q,db='postgres'):
        return run('exec',name,BIN+'/psql','-X','-v','ON_ERROR_STOP=1','-At','-h','/tmp','-U','postgres','-d',db,'-c',q).stdout.strip()
    def ready():
        for _ in range(30):
            require(inspect(name)['State']['Running'],'Candidate exited before ready')
            if run('exec',name,BIN+'/pg_isready','-h','/tmp','-U','postgres',check=False).returncode==0: return
            time.sleep(1)
        raise RuntimeError('Candidate did not become ready')
    def stop():
        if inspect(name)['State']['Running']: run('kill','--signal=SIGINT',name)
        for _ in range(120):
            state=inspect(name)['State']
            if not state['Running']:
                require(state['ExitCode']==0,'Candidate did not exit cleanly')
                return
            time.sleep(1)
        raise RuntimeError('Shutdown timed out; no SIGKILL sent')
    try:
        run('start',name);ready()
        version=int(sql('SHOW server_version_num'))
        require(160006<=version<170000,'Expected PostgreSQL 16.6 or newer within 16.x')
        require(sql('SHOW lc_collate')=='C.UTF-8','Unexpected locale')
        sql('CREATE TABLE public.ci_probe(id integer PRIMARY KEY, value text NOT NULL); INSERT INTO public.ci_probe VALUES (1,\'persisted\');')
        run('exec',name,BIN+'/pg_dump','-h','/tmp','-U','postgres','-Fc','-f','/tmp/probe.dump','postgres')
        run('exec',name,BIN+'/createdb','-h','/tmp','-U','postgres','ci_restore')
        run('exec',name,BIN+'/pg_restore','--exit-on-error','-h','/tmp','-U','postgres','-d','ci_restore','/tmp/probe.dump')
        require(sql('SELECT value FROM ci_probe WHERE id=1','ci_restore')=='persisted','Restore data mismatch')
        stop();run('start',name);ready()
        require(sql('SELECT value FROM ci_probe WHERE id=1')=='persisted','Restart data mismatch')
        print('PG_VERSION_NUM='+str(version))
        print(run('exec',name,'cat','/usr/local/share/ac-postgresql/version.txt').stdout,end='')
        success=True
    finally:
        print(run('logs','--tail=80',name,check=False).stdout,end='')
        stop()
        if success: run('rm',name)
        else: print('TEST_CONTAINER_RETAINED: '+name,file=sys.stderr)
    print('PG_IMAGE_TEST_OK: '+image)

if __name__=='__main__':
    def interrupted(sig,frame): raise RuntimeError('Interrupted')
    for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,interrupted)
    try: main()
    except Exception as e:
        print('PG_IMAGE_TEST_FAILED: '+str(e)+'; '+str(getattr(e,'stderr',''))[-3000:],file=sys.stderr)
        sys.exit(1)
