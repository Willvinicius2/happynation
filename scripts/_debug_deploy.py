import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
print("[t0]", time.strftime("%H:%M:%S"), "starting", flush=True)
print("[t1] importing config", flush=True)
from src.research.aether.config import db_path
print("  db_path:", db_path(), flush=True)
print("[t2] importing active", flush=True)
t = time.time()
from src.research.active import deploy_record, list_active, remove
print(f"  active import: {time.time()-t:.1f}s", flush=True)
print("[t3] importing auto_deploy", flush=True)
t = time.time()
from src.research.aether import auto_deploy
print(f"  auto_deploy import: {time.time()-t:.1f}s", flush=True)
print(f"  MIN_SCORE: {auto_deploy.MIN_SCORE}", flush=True)
print(f"  MAX_PER_HYPOTHESIS_ASSET: {auto_deploy.MAX_PER_HYPOTHESIS_ASSET}", flush=True)
print("[t4] running run_once", flush=True)
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("aether.auto_deploy")
log.setLevel(logging.INFO)
t = time.time()
result = auto_deploy.run_once(log)
print(f"[t5] run_once finished in {time.time()-t:.1f}s -> {result}", flush=True)
