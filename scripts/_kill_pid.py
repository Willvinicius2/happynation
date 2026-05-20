import sys, psutil, time
pids = [int(x) for x in sys.argv[1:]]
for pid in pids:
    try:
        p = psutil.Process(pid)
        p.kill()
        print(f"killed {pid}")
    except psutil.NoSuchProcess:
        print(f"{pid} already gone")
    except Exception as e:
        print(f"err {pid}: {e}")
time.sleep(0.8)
for pid in pids:
    try:
        psutil.Process(pid)
        print(f"{pid} STILL ALIVE")
    except psutil.NoSuchProcess:
        print(f"{pid} dead")
