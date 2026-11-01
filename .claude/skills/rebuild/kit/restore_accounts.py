"""Put the saved accounts and login sessions back after the rebuild — by key,
UPSERT, so the scraper account finish-import re-created is matched, not doubled."""
import json, os, sys
KIT = os.path.dirname(os.path.abspath(__file__))   # the kit folder this copy lives in
os.chdir("/home/administrator/github/pamten-backend/backend"); sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv(".env")
from app.db.arcadedb import run_sql
saved = json.load(open(os.path.join(KIT, "accounts.json")))
for table, key in (("User", "email"), ("RefreshToken", "id")):
    n = 0
    have = {r[key] for r in run_sql(f"SELECT `{key}` FROM {table}")}
    for row in saved[table]:
        sets = ", ".join(f"`{k}` = :p_{k}" for k in row)
        params = {f"p_{k}": v for k, v in row.items()}
        # by key: UPDATE where the row exists, INSERT where it does not
        # (UPSERT needs an index, and RefreshToken has none on `id`)
        if row[key] in have:
            run_sql(f"UPDATE {table} SET {sets} WHERE `{key}` = :p_{key}", params)
        else:
            run_sql(f"INSERT INTO {table} SET {sets}", params)
        n += 1
    print(f"{table}: {n} restored; now {run_sql(f'SELECT count(*) AS n FROM {table}')[0]['n']} rows")
