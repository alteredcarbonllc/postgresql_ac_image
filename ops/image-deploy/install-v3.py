#!/usr/bin/python3
"""Upgrade known v2 tools atomically per file; never restart or deploy containers."""
import ast
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile

BASE=Path(__file__).resolve().parent
LIB=Path('/usr/local/libexec/ac-postgresql')
STATE=Path('/var/lib/ac-postgresql')
WRAPPER=Path('/usr/local/sbin/ac-pg-image')

def require(ok,msg):
    if not ok: raise RuntimeError(msg)

def main():
    require(os.geteuid()==0,'Run as root')
    os.umask(0o077)
    names=('ac-pg-runtime.py','config-deploy.py')
    sources={n:(BASE/n).read_bytes() for n in names}
    for n in names: ast.parse(sources[n],filename=n)
    wrapper=(BASE/'ac-pg-image').read_bytes()
    with (STATE/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        require(not (STATE/'pending-config-deploy.json').exists(),'Unfinished deployment')
        require(not Path('/etc/ac/runit/postgresql/down').exists(),'Supervisor is down')
        installed={n:(LIB/n).read_bytes() for n in names}
        if installed==sources and WRAPPER.is_file() and WRAPPER.read_bytes()==wrapper:
            print('TOOLS_V3_ALREADY_INSTALLED: no changes')
            return
        require(installed['ac-pg-runtime.py']==(BASE/'runtime-v2.py').read_bytes(),'Unexpected runtime; refusing overwrite')
        require(installed['config-deploy.py']==(BASE/'config-deploy-v2.py').read_bytes(),'Unexpected config deployer; refusing overwrite')
        require(not WRAPPER.exists() and not WRAPPER.is_symlink(),'Unexpected ac-pg-image wrapper')
        spec=importlib.util.spec_from_file_location('old',LIB/'ac-pg-runtime.py')
        old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
        old.load_active();old.healthy()
        require(old.MARKER.is_file() and all(old.script_status()),'Legacy exclusion guards missing')
        before=(STATE/'active.json').read_bytes()
        saved=Path(tempfile.mkdtemp(prefix='image-tool-upgrade.',dir=STATE))
        for n,data in installed.items(): old.atomic(saved/n,data)
        old.atomic(saved/'active.json',before)
        old.atomic(saved/'checksums.json',json.dumps({n:hashlib.sha256(d).hexdigest() for n,d in installed.items()},indent=2).encode())
        try:
            # New runtime accepts the old manifest and old deployer throughout.
            for n in names: old.atomic(LIB/n,sources[n],0o644)
            old.atomic(WRAPPER,wrapper,0o755)
            require((STATE/'active.json').read_bytes()==before,'Active state changed during tool install')
            old.healthy()
        except BaseException:
            for n,data in installed.items(): old.atomic(LIB/n,data,0o644)
            WRAPPER.unlink(missing_ok=True)
            raise
        print('TOOLS_V3_INSTALLED: no container restart; backup='+str(saved))

if __name__=='__main__': main()
