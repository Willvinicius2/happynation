import psutil, os
me = os.getpid()
killed = []
for p in psutil.process_iter(['pid','name','cmdline']):
    try:
        if p.info['pid'] == me:
            continue
        if (p.info['name'] or '').lower() != 'python.exe':
            continue
        argv = p.info['cmdline'] or []
        joined = ' '.join(argv)
        target = any(s in joined for s in [
            '_debug_deploy.py',
            '_list_aether.py',
            'multiprocessing.spawn',
            '_kill_pid.py',
            '_kill_autodeploy.py',
            '_cleanup_zombies.py',
        ])
        if not target and '-m src.research.aether.auto_deploy' in joined:
            target = True
        if target:
            try:
                p.kill()
                killed.append(p.info['pid'])
            except Exception:
                pass
    except Exception:
        pass
print("killed:", killed)
