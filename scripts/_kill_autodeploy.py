import psutil, os, time
me = os.getpid()
to_kill = []
for p in psutil.process_iter(['pid','cmdline']):
    try:
        if p.info['pid'] == me:
            continue
        argv = p.info['cmdline'] or []
        # Match only if `-m` is followed by exact module name in argv
        for i, tok in enumerate(argv):
            if tok == "-m" and i + 1 < len(argv) and argv[i+1].strip() == "src.research.aether.auto_deploy":
                to_kill.append(p)
                break
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
print("matches:", [p.pid for p in to_kill])
for p in to_kill:
    try:
        p.kill()
        print("killed", p.pid)
    except Exception as e:
        print("err", p.pid, e)
time.sleep(1.5)
left = []
for p in psutil.process_iter(['pid','cmdline']):
    try:
        if p.info['pid'] == me: continue
        argv = p.info['cmdline'] or []
        for i, tok in enumerate(argv):
            if tok == "-m" and i + 1 < len(argv) and argv[i+1].strip() == "src.research.aether.auto_deploy":
                left.append(p.info['pid'])
                break
    except:
        pass
print("left:", left)
