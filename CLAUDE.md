# key-tool — activation-key generator (Python)

Standalone ops tool: bulk-mint and track CODEGNITO activation keys in the prod D1 (`../codegnito_backend`). Pure Python stdlib + the `wrangler` in `../codegnito_backend`. Own repo: `github.com/codegnito/codegnito-key-tool`. See `README.md` for full usage; this file is the quick map.

## Usage
- `python make_keys.py <count> [--apply]` — generate N keys (default 500, 30-day); `--apply` also INSERTs into D1.
- `python make_keys.py --reconcile` — sync the CSV ledger with D1 (mark used/unused, import D1-only keys like the 13 manual ones). Run first-time and any time.
- `python make_keys.py --export-sql` — regenerate per-batch `.sql` in `out/` from the ledger (no DB).
- Other flags: `--days`, `--batch`, `--local`, `--no-db-check`, `--db`, `--account`.

## Key facts
- **Uniqueness guaranteed across runs**: before generating, loads every key from D1 *and* the ledger and avoids all of them; inserts use `INSERT OR IGNORE`. The D1 check runs every time unless `--no-db-check`.
- Key format `XXXX-XXXX-XXXX-XXXX`, alphabet `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` (no I/O/0/1) — matches the Worker validator, so every key activates. A key = 1500 lifetime calls, 30-day from first activation, HWID-locked.
- D1 reads use `wrangler ... --command` (NOT `--file`: `--file --remote` returns exec stats, not SELECT rows); output is ANSI-stripped + regex-extracted, parsed as `[0].results`.
- Targets prod D1 `codegnito-backend-apac` (account `d677090c…`) by default; `--local` hits the dev emulator.
- **`out/` is sensitive** — generated keys + `keys-master.csv` ledger + batch SQL. It is **git-ignored; never commit it, never delete it.** Ledger columns: generated (`key,duration_days,created_at,batch`) + you-maintained (`status,shared_to,shared_date,notes`) + reconcile-set (`db_used,db_activated_at,db_calls_used`).
- Output is ASCII-only (Windows cp1252 console) — keep it that way.
