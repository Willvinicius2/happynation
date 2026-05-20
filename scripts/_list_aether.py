import psutil, json
out = []
for p in psutil.process_iter(['pid','ppid','name','cmdline']):
    try:
        if (p.info['name'] or '').lower() != 'python.exe':
            continue
        argv = p.info['cmdline'] or []
        if any('aether' in (a or '') or 'src.main' in (a or '') or 'src.dashboard' in (a or '') for a in argv):
            out.append({'pid': p.info['pid'], 'ppid': p.info['ppid'], 'argv': argv})
    except:
        pass
for o in out:
    print(f"pid={o['pid']} ppid={o['ppid']} argv={o['argv']}")
print(f"total: {len(out)}")
