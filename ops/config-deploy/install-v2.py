#!/usr/bin/python3
"""Install stage 2 tooling without restarting PostgreSQL."""
import ast, fcntl, importlib.util, json, os
from pathlib import Path
import tempfile
base=Path(__file__).resolve().parent
lib=Path('/usr/local/libexec/ac-postgresql')
state=Path('/var/lib/ac-postgresql')
assert os.geteuid()==0, 'Run as root'
os.umask(0o077)
with (state/'lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    assert not (state/'active.json').exists(), 'Already installed; do not overwrite active state'
    assert (lib/'ac-pg-runtime.py').read_bytes()==(base/'runtime-v1.py').read_bytes(), 'Unexpected installed runtime'
    assert not Path('/etc/ac/runit/postgresql/down').exists(), 'Service is down'
    for name in ('ac-pg-runtime.py','config-deploy.py'):
        ast.parse((base/name).read_text())
    spec=importlib.util.spec_from_file_location('old',lib/'ac-pg-runtime.py')
    old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    old.require(old.MARKER.exists(),'Not adopted')
    old.healthy()
    old.require(all(old.script_status()),'Legacy guards missing')
    c=old.inspect()
    saved=Path(tempfile.mkdtemp(prefix='tool-upgrade.',dir=state))
    (saved/'runtime-v1.py').write_bytes((lib/'ac-pg-runtime.py').read_bytes())
    (saved/'inspect.json').write_text(json.dumps(c,indent=2))
    active={'id':c['Id'],'image':old.IMAGE,'cmd':c['Config']['Cmd'],
            'mounts':old.MOUNTS,'config_commit':None}
    # Manifest first: old runtime ignores it; new runtime requires it.
    old.atomic(state/'active.json',(json.dumps(active,indent=2)+'\n').encode())
    for name in ('ac-pg-runtime.py','config-deploy.py'):
        old.atomic(lib/name,(base/name).read_bytes(),0o644)
    old.atomic(Path('/usr/local/sbin/ac-pg-config'),(base/'ac-pg-config').read_bytes(),0o755)
    print('TOOLS_V2_INSTALLED: no restart; backup='+str(saved))
