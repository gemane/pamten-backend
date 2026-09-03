"""Former register identities, recovered from GLEIF's snapshot archive.

The register a company sits in is treated everywhere as a hard identifier —
and it can CHANGE: Tesla re-registered from Delaware to Texas in 2024, and the
current golden copy no longer knows Delaware ever applied. A source that knew
the company under its previous register (the Companies House PSC filer still
says Delaware) then has no key to merge on, and the dedup rightly leaves two
nodes standing.

GLEIF publishes complete snapshots three times a day back to 2018-02-09, so
the history is recoverable: stream any historical snapshot's CSV, and wherever
its register pair differs from what the entity carries today, keep the old
pair in ``former_register_ids``. The list joins the hard-id dedup (see
maintenance._former_register_groups), and the daily delta keeps it growing —
import_lei_cdf_delta preserves the outgoing pair whenever a delta moves one.

Manual command (`manage.py backfill-former-registers`), local files only: a
multi-hundred-MB download must never start as a side effect of a backfill.
"""
import csv
import io
import logging
import zipfile

from app.db.arcadedb import run_sql
from app.scraper.gleif_reference import make_register_id

log = logging.getLogger(__name__)

# Golden-copy lei2 CSV column names (stable across CDF 2.x/3.x exports).
_COL_LEI = "LEI"
_COL_RA = "Entity.RegistrationAuthority.RegistrationAuthorityID"
_COL_NUM = "Entity.RegistrationAuthority.RegistrationAuthorityEntityID"


def register_pair_from_row(row: dict) -> tuple[str | None, str | None]:
    """(lei, register_id) from one snapshot CSV row — None where absent/placeholder."""
    lei = (row.get(_COL_LEI) or "").strip().upper() or None
    rid = make_register_id(row.get(_COL_RA), row.get(_COL_NUM))
    return lei, rid


def _open_snapshot_csv(path: str):
    """The CSV stream inside a golden-copy zip (or a bare .csv path)."""
    if path.endswith(".zip"):
        zf = zipfile.ZipFile(path)
        member = next(n for n in zf.namelist() if n.endswith(".csv"))
        return io.TextIOWrapper(zf.open(member), encoding="utf-8-sig")
    return open(path, encoding="utf-8-sig")  # noqa: SIM115 - closed by caller


def backfill_former_registers(paths: list[str]) -> dict:
    """Stream historical snapshots over the entities this database holds.

    The database side is read once into a dict (id-indexed; a curated subset is
    tiny and even full GLEIF fits a mapping); each snapshot streams row by row,
    so file size never matters. Only entities present HERE are touched — the
    snapshot describes the whole world, this database rarely does.
    """
    holdings: dict[str, dict] = {}
    for r in run_sql("SELECT id, lei_id, register_id, former_register_ids "
                     "FROM Entity WHERE lei_id IS NOT NULL"):
        d = dict(r)
        holdings[(d["lei_id"] or "").upper()] = {
            "id": d["id"],
            "register_id": d.get("register_id"),
            "former": set(d.get("former_register_ids") or []),
        }

    counts = {"snapshots": 0, "rows": 0, "matched": 0, "backfilled": 0}
    pending: dict[str, dict] = {}
    for path in paths:
        counts["snapshots"] += 1
        fh = _open_snapshot_csv(path)
        try:
            for row in csv.DictReader(fh):
                counts["rows"] += 1
                lei, rid = register_pair_from_row(row)
                if not lei or not rid:
                    continue
                node = holdings.get(lei)
                if node is None:
                    continue
                counts["matched"] += 1
                # Same pair as today (or already recorded) → nothing historical.
                if rid == node["register_id"] or rid in node["former"]:
                    continue
                node["former"].add(rid)
                pending[node["id"]] = node
        finally:
            fh.close()

    for node in pending.values():
        run_sql("UPDATE Entity SET former_register_ids = :l WHERE id = :id",
                {"l": sorted(node["former"]), "id": node["id"]})
        counts["backfilled"] += 1
    log.info("former-register backfill: %s", counts)
    return counts
