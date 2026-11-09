"""Every LEI the graph holds now — one per line, for `gleif-succession --only-file`
and `gleif-lei-cdf --only-file` (findings item 89).
Usage: lei_ids.py <out.txt>   (read-only)

The import chain reads the successions of the import's own LEIs (~2,000); the
replay then adds ~600 `lei:` nodes (the register pointer, the securities
bridge, Exhibit 21 confirmations) whose successions nobody read — 84 of the
86 pairs of the 2026-11-08 guard touched dev's final LEI set, 46 the import's.
Step 7 writes this list into the kit folder (the chain's mktemp files in /tmp
were gone when the verify needed them) and runs the succession step again.

`ids(q)` takes a SQL runner, so a test can hand it a fake; `main()` runs it
against the dev database."""
import os
import sys

BACKEND = "/home/administrator/github/pamten-backend/backend"


def ids(q) -> list[str]:
    """The bare LEIs of every `lei:` node, sorted; a merged-away node is no
    longer a node, so a predecessor folded into its live record is not listed
    twice."""
    out = {r["id"][4:] for r in q("SELECT id FROM Entity WHERE id LIKE 'lei:%'") or []
           if len(r["id"]) == 24}
    return sorted(out)


def main() -> None:
    out = sys.argv[1]
    os.chdir(BACKEND); sys.path.insert(0, ".")
    from dotenv import load_dotenv; load_dotenv(".env")
    from app.db.arcadedb import run_sql as q
    leis = ids(q)
    with open(out, "w") as f:
        f.write("# every lei: node on dev after the replay (kit step 7, findings item 89)\n")
        f.write("\n".join(leis) + "\n")
    print(f"{len(leis)} LEIs -> {out}")


if __name__ == "__main__":
    main()
