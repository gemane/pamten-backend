"""Diff two rebuild snapshots for `/finding verify`.
Usage: diff_snapshots.py <previous after.json> <new after.json>

Counts per key, hard-id nodes gone and new, uuid nodes by (name, source), and
every OWNS edge by a stable key — uuid ids change with every rebuild, so an
edge is named by its ends' hard ids or, failing one, their names.

A 13F edge whose filer node exists only on the side the edge exists on is the
relevance sample's boundary moving (findings item 51: Dixon Financial, rank
100 of the 100 non-known filers the 13F run reads, present on 11-02 and gone
on 11-03 with its node), not a lost holder; it is reported apart, as expected
churn. A 13F edge from a filer the graph knows on both sides is a finding
(item 40 lost 46 of them). Snapshots written before findings item 51 carry no
filing type on their edges, and every edge of theirs is judged as a finding."""
import collections
import json
import sys

OWNS_FIELDS = ("owner", "owned", "source", "until", "filing_type")


def load(path: str) -> dict:
    return json.load(open(path))


def _is_uuid(i: str) -> bool:
    return ":" not in i


def group_key(row: list) -> str:
    """A VotingGroup's stable key: its name key (which carries the issuer) and
    its members, sorted — the roster is the group's identity, and one issuer can
    carry several blocs (findings item 56)."""
    return f"{row[2]} [{', '.join(row[3])}]"


def names(snap: dict) -> dict:
    """node id → the key an edge end is named by: a hard id as it is, a uuid by
    its name, a VotingGroup (a uuid re-minted by every rebuild) by its issuer
    and roster. A snapshot from before item 56 lists no groups, and their
    edges keep the raw uuid as before."""
    m = {r[0]: (r[0] if not _is_uuid(r[0]) else r[1]) for r in snap["entities"] + snap["persons"]}
    m.update({r[0]: group_key(r) for r in snap.get("voting_groups", [])})
    return m


def edge(row: list) -> dict:
    """One owns_edges row, four fields (before item 51) or five, as a dict."""
    return dict(zip(OWNS_FIELDS, [*row, *([None] * (len(OWNS_FIELDS) - len(row)))]))


def edge_keys(snap: dict) -> collections.Counter:
    m = names(snap)
    return collections.Counter(
        (m.get(e["owner"], e["owner"]), m.get(e["owned"], e["owned"]), e["source"], e["until"] is None, e["filing_type"])
        for e in map(edge, snap["owns_edges"]))


def is_13f(filing_type) -> bool:
    return bool(filing_type) and str(filing_type).upper().startswith("13F")


def classify(prev: dict, new: dict) -> dict:
    """{'gone', 'new', 'churn_gone', 'churn_new'}: edge key → count. The churn
    lists hold the 13F edges whose filer node exists only where the edge does."""
    kp, kn = edge_keys(prev), edge_keys(new)
    owners_p = set(names(prev).values()) | set(names(prev))
    owners_n = set(names(new).values()) | set(names(new))
    out = {"gone": collections.Counter(), "new": collections.Counter(),
           "churn_gone": collections.Counter(), "churn_new": collections.Counter()}
    for k, n in (kp - kn).items():
        side = "churn_gone" if is_13f(k[4]) and k[0] not in owners_n else "gone"
        out[side][k] += n
    for k, n in (kn - kp).items():
        side = "churn_new" if is_13f(k[4]) and k[0] not in owners_p else "new"
        out[side][k] += n
    return out


def _print_counts(P: dict, N: dict) -> None:
    for k in P:
        a, b = P[k], N.get(k)
        if isinstance(a, int) or (isinstance(a, dict) and all(isinstance(v, (int, dict)) for v in a.values())):
            if a != b:
                if isinstance(a, dict):
                    keys = sorted(set(a) | set(b or {}), key=str)
                    d = {x: (a.get(x), (b or {}).get(x)) for x in keys if a.get(x) != (b or {}).get(x)}
                    print(f"{k}: {d}")
                else:
                    print(f"{k}: {a} -> {b}")


def _print_nodes(P: dict, N: dict) -> None:
    for kind in ("entities", "persons"):
        a = {r[0]: r for r in P[kind]}; b = {r[0]: r for r in N[kind]}
        print(f"\n== {kind}: {len(a)} -> {len(b)}")
        for i in sorted(set(a) - set(b)):
            if not _is_uuid(i): print("  -", a[i])
        for i in sorted(set(b) - set(a)):
            if not _is_uuid(i): print("  +", b[i])
        ua = collections.Counter((r[1], r[2]) for r in P[kind] if _is_uuid(r[0]))
        ub = collections.Counter((r[1], r[2]) for r in N[kind] if _is_uuid(r[0]))
        print(f"  uuid by (name, source): only before {sum((ua - ub).values())}, only now {sum((ub - ua).values())}")
        for k2, n in sorted((ua - ub).items(), key=str): print("  u-", k2, n)
        for k2, n in sorted((ub - ua).items(), key=str): print("  u+", k2, n)
        for x, y in sorted((a[i], b[i]) for i in set(a) & set(b) if a[i][3:] != b[i][3:]):
            print("  ~", x, "->", y[3:])
    # groups by issuer + roster: a roster that changed shows as one gone, one new
    ga = collections.Counter(group_key(r) for r in P.get("voting_groups", []))
    gb = collections.Counter(group_key(r) for r in N.get("voting_groups", []))
    print(f"\n== voting groups: {sum(ga.values())} -> {sum(gb.values())}")
    for k, n in sorted((ga - gb).items()): print("  -", k, n)
    for k, n in sorted((gb - ga).items()): print("  +", k, n)


def _print_edges(P: dict, N: dict) -> None:
    c = classify(P, N)
    m = names(N)
    print(f"\n== OWNS edges: gone {sum(c['gone'].values())}, new {sum(c['new'].values())}; "
          f"13F sample churn (expected): gone {sum(c['churn_gone'].values())}, new {sum(c['churn_new'].values())}")
    for k, n in sorted(c["gone"].items(), key=str): print("  -", k, n)
    for k, n in sorted(c["new"].items(), key=str): print("  +", k, n)
    by_owned = collections.Counter(m.get(k[1], k[1]) for k in c["new"].elements())
    print("  new by owned:", by_owned.most_common(30))
    by_owner = collections.Counter(m.get(k[0], k[0]) for k in c["new"].elements())
    print("  new by owner:", by_owner.most_common(15))
    churn_by_issuer = collections.Counter()
    for k, n in c["churn_gone"].items(): churn_by_issuer[(m.get(k[1], k[1]), "gone")] += n
    for k, n in c["churn_new"].items(): churn_by_issuer[(m.get(k[1], k[1]), "new")] += n
    print("  13F sample churn by issuer:", sorted(churn_by_issuer.items(), key=str))


def _print_source_calls(P: dict, N: dict) -> None:
    """Calls per source and kind, previous vs new — not a finding by itself (a
    warm or cold bucket moves it), but a source asked far more often than last
    time is worth a look."""
    a, b = P.get("source_calls") or {}, N.get("source_calls") or {}
    if not a and not b:
        return
    print("\nsource calls (previous -> new):")
    for src in sorted(set(a) | set(b)):
        kinds = sorted(set(a.get(src, {})) | set(b.get(src, {})))
        print(f"  {src:22} " + ", ".join(
            f"{k} {a.get(src, {}).get(k, '-')} -> {b.get(src, {}).get(k, '-')}" for k in kinds))


def main() -> None:
    P, N = load(sys.argv[1]), load(sys.argv[2])
    _print_counts({k: v for k, v in P.items() if k != "source_calls"}, N)
    _print_source_calls(P, N)
    _print_nodes(P, N)
    for k in ("lei_and_cik", "wikidata_entities", "users"):
        a, b = set(map(str, P[k])), set(map(str, N[k]))
        print(f"\n{k}: -{sorted(a - b)} +{sorted(b - a)}")
    _print_edges(P, N)


if __name__ == "__main__":
    main()
