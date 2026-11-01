"""Save the accounts and login sessions before the drop (read-only)."""
import json, os, sys
KIT = os.path.dirname(os.path.abspath(__file__))   # the kit folder this copy lives in
os.chdir("/home/administrator/github/pamten-backend/backend"); sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv(".env")
from app.db.arcadedb import run_sql
out = {t: [{k: v for k, v in r.items() if not k.startswith("@")} for r in run_sql(f"SELECT FROM {t}")]
       for t in ("User", "RefreshToken")}
path = os.path.join(KIT, "accounts.json")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(out, f, default=str)
print({t: len(v) for t, v in out.items()})
