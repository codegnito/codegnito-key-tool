# Codegnito — Activation Key Tool

Bulk-generate activation keys, keep a CSV record (with sharing tracking), and
insert them into the production D1 `activation_keys` table.

Pure Python (stdlib only). Uses the `wrangler` already installed in
`../codegnito_backend` to talk to D1, and your existing `wrangler login`.

## Uniqueness is guaranteed across runs

Before generating, the tool loads **every existing key from D1** (your manually
created keys + all previously applied batches) **and** from the local ledger,
then generates new keys that avoid all of them. So a 2nd run never collides with
the 1st, your manual keys, or anything already in the DB. (Inserts also use
`INSERT OR IGNORE` as a final safety net.) The D1 check runs every time — skip it
only with `--no-db-check`.

**First time:** run `python make_keys.py --reconcile` once — it pulls your
existing keys (e.g. your 13 manual ones) into the ledger CSV so everything is
tracked in one place.

## Generate (review only — does NOT touch the DB)

```powershell
cd d:\Codegnito\key-tool
python make_keys.py 500          # 500 keys, 30-day
python make_keys.py 3            # quick test with 3
python make_keys.py 10           # quick test with 10
```

Writes to `./out/`:
- `keys-<batch>.csv`  — this run's keys + sharing columns
- `keys-<batch>.sql`  — the exact INSERT statements
- `keys-master.csv`   — **append-only ledger of every key** (search this)

## Generate AND insert into prod D1

```powershell
python make_keys.py 500 --apply
```

Targets prod by default (DB `codegnito-backend-apac`, account `d677090c…`).
Add `--local` to hit the local dev emulator instead.

## Reconcile (which keys got used?)

```powershell
python make_keys.py --reconcile
```

Reads `keys-master.csv`, queries D1, and (a) updates each key's `db_*` columns
and (b) **imports any keys that exist in D1 but not the ledger** (e.g. your
manual keys) — without touching your `shared_to`/`status` edits. Prints a
summary. Run any time.

## Options

| Arg | Default | Meaning |
|---|---|---|
| `count` (positional) | 500 | how many keys to generate |
| `--days N` | 30 | validity per key |
| `--batch NAME` | `batch-<UTC ts>` | label (-> DB `notes` + CSV `batch` column) |
| `--apply` | off | also INSERT into D1 |
| `--reconcile` | off | sync the ledger with D1 (no generation) |
| `--export-sql` | off | regenerate per-batch `.sql` files in `out/` from the ledger (no DB) |
| `--local` | off | use the local dev D1 instead of `--remote` |
| `--no-db-check` | off | skip the D1 uniqueness query (ledger-only; offline) |
| `--db NAME` | `codegnito-backend-apac` | D1 database name |
| `--account ID` | prod account | Cloudflare account (or set `CLOUDFLARE_ACCOUNT_ID`) |

## Tracking who got which key

`out/keys-master.csv` columns:

```
key, duration_days, created_at, batch,            <- generated
status, shared_to, shared_date, notes,            <- YOU fill as you hand keys out
db_used, db_activated_at, db_calls_used           <- set by --reconcile (from D1)
```

- **You** maintain `status` / `shared_to` / `shared_date` (set `status=shared`,
  `shared_to=…` when you give a key away).
- **`--reconcile`** maintains `db_used` (`used` = activated, `unused` = not yet,
  `MISSING` = not in the DB), plus when it was activated and how many of its 1500
  calls are spent.

So you see the full lifecycle — **unshared -> shared -> used** — and which keys
are spare vs consumed. Keep this file private; `out/` is git-ignored.

## Notes

- Key format `XXXX-XXXX-XXXX-XXXX`, alphabet `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`
  (no I/O/0/1) — matches the Worker validator, so every generated key activates.
- A key is **1500 lifetime AI calls**, **expires `--days` after first activation**,
  hardware-locked to the first device that activates it.
