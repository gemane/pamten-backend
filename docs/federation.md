# Federation — design notes

How independent Pamten instances, run by different people on different servers,
share ownership data as **trusted peers**. Implementation: `app/routers/federation.py`,
`app/federation_keys.py`, `app/models/federation.py`. Gated by `FEDERATION_ENABLED`.

## Model: exchange, not replication

Federation is **one-way and opt-in**. An instance *publishes* a snapshot of its
graph; a peer *pulls* it. Nothing is pushed to you, nothing syncs automatically,
and a peer being offline, wrong, or malicious can never corrupt your graph —
worst case you distrust a source.

Two principles make this safe:

- **Reconcile, don't overwrite.** Pulled nodes are matched to yours on external
  ids and folded in; pulled facts are attributed to the peer, never blindly
  merged into yours (keep-all-claims). You decide what to trust at read time via
  each `Source`'s credibility.
- **Verify provenance.** Exports are signed; a pull from a peer whose key you
  hold is cryptographically verified (below).

### Why a native snapshot, not BODS?

The obvious choice would be [BODS](https://standard.openownership.org/) — we
already import it. But **our BODS importer only carries LEI and Companies-House
ids** and drops `wikidata_id` / `sec_cik`, which are the *main* identity anchors
for the graph. Round-tripping through BODS would lose the very keys federation
depends on for reconciliation. So the export is a compact native format that
preserves **every** external id. It maps cleanly back to BODS later if interop is
wanted.

## The snapshot — `GET /federation/export`

```jsonc
{
  "format": "pamten-federation", "version": 1, "generated_at": "…",
  "entities":  [ { "name", "type", "country", "founded",
                   "wikidata_id", "sec_cik", "lei_id", "companies_house_id" } ],
  "persons":   [ { "full_name", "first_name", "last_name",
                   "wikidata_id", "sec_cik", "birth_date", "birth_place", "nationality" } ],
  "ownerships":[ { "owner": {ref}, "owned": {ref},
                   "stake_percent", "ownership_type", "source_url", "source_date" } ],
  // signature envelope (present only when a signing key is configured):
  "algorithm": "ed25519", "key_id": "…", "signature": "…"
}
```

A `ref` is `{kind: entity|person, wikidata_id, sec_cik, lei_id, companies_house_id, name}`
— enough for the puller to resolve the endpoint. **Scope is deliberately minimal:
`Entity` + `Person` nodes and `OWNS` edges only.** Roles, locations, and the
Source graph are intentionally out for now.

## Signing — verifiable provenance (step 2)

Each instance holds an **Ed25519** signing key (`FEDERATION_SIGNING_KEY`, a base64
32-byte seed, kept in env; generate with `python manage.py gen-federation-key`).

- **Signature** — detached, over the *canonical* JSON of the snapshot: all fields
  except the envelope (`signature`/`key_id`/`algorithm`), serialized with sorted
  keys and compact separators, so signer and verifier hash identical bytes
  (`federation_keys.canonical`).
- **`key_id`** — `sha256(public_key)[:16]`, a short stable fingerprint.
- **Publishing** — `GET /federation/public-key` returns the public key + `key_id`
  for peers to register. If no signing key is set the export is simply unsigned
  and `signing_enabled` is `false` (back-compatible).

## Peers — `POST /federation/peers`

A `Peer` records `name`, `base_url`, `credibility_score`, an optional
`auth_token` (bearer for the peer's export endpoint), and the peer's
`public_key`. Tokens and keys are **never returned** by `GET /federation/peers`
(it reports `has_token` / `has_public_key` only).

## The pull — `POST /federation/peers/{id}/pull`

1. **Fetch** `{base_url}/federation/export` (with the peer's `auth_token` as a
   bearer, if set).
2. **Verify** — if the peer's `public_key` is on file, check the signature. A
   mismatch is **refused with 422** ("not provably from this peer"). A peer with
   no key still imports, but is marked **unverified**.
3. **Import** (`import_snapshot`) — upsert each node, **reconciled on external id**
   (`wikidata_id` → `sec_cik` → `lei_id` → `companies_house_id`) and falling back
   to normalized name; write `OWNS` edges. Everything is stamped with a
   `Peer: <name>` `Source` carrying the peer's `credibility_score` plus
   `verified` / `key_id`.
4. **Reconcile** — run `deduplicate_high_confidence()` (the same
   [duplicate scan](deduplication.md)) so a peer's "Larry Fink" folds into yours
   rather than duplicating.

The response reports `{peer, verified, imported: {...counts}, deduplication: {...}}`.

## Trust & threat model

Trusted-peer, not open contribution. The threats handled:

| Threat | Mitigation |
|---|---|
| Fabricated data claiming to be a peer's | Ed25519 signature verified against the peer's registered public key |
| Tampering in transit | Signature covers the canonical snapshot; any change invalidates it |
| A peer you no longer trust | Drop/downgrade its `Source`; your own nodes are untouched |
| Colliding identity across instances | Reconcile on external ids, then the fuzzy duplicate scan |

Not (yet) handled: retraction/tombstones (a pulled fact isn't auto-removed if the
peer later deletes it), and non-repudiation beyond the key you were given
out-of-band.

## ⚠️ GDPR

Federating **person** data (names, birth dates, birthplaces) across operators and
jurisdictions is a data-protection consideration independent of the tech —
controller/processor roles, lawful basis, cross-border transfer. Enable
deliberately.

## Endpoint summary

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /federation/status` | contributor | On/off + publish counts |
| `GET /federation/export` | contributor | The (signed) snapshot |
| `GET /federation/public-key` | contributor | This instance's key + `key_id` |
| `GET /federation/peers` | contributor | List peers (tokens/keys masked) |
| `POST /federation/peers` | admin | Register a trusted peer |
| `DELETE /federation/peers/{id}` | admin | Remove a peer |
| `POST /federation/peers/{id}/pull` | admin | Pull, verify, import, reconcile |

## Setup quickstart

```bash
# 1. enable + generate a signing key, set it in the env, redeploy
python manage.py gen-federation-key      # → FEDERATION_SIGNING_KEY + public key

# 2. share your public key (from GET /federation/public-key) with a peer
# 3. register the peer and pull
curl -X POST "$API/federation/peers" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Partner","base_url":"https://partner.example.com",
       "auth_token":"<their token>","public_key":"<their key>","credibility_score":70}'
curl -X POST "$API/federation/peers/<id>/pull" -H "Authorization: Bearer $TOKEN"
```
