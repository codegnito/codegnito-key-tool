#!/usr/bin/env python3
"""
Codegnito activation-key tool — bulk generate + reconcile.

Uniqueness is GUARANTEED across runs: before generating, every existing key is
loaded from D1 (your manual keys + all applied batches) AND from the local
ledger, and new keys are generated to avoid all of them. Accuracy first —
the D1 check runs every time (skip it only with --no-db-check).

USAGE (run from this folder):
    python make_keys.py 500            # 500 keys, 30-day -> CSV + SQL only (review)
    python make_keys.py 10             # quick test with 10
    python make_keys.py 3              # quick test with 3
    python make_keys.py 500 --apply    # ALSO insert into prod D1
    python make_keys.py 10 --apply --local        # against the LOCAL dev D1
    python make_keys.py 50 --batch march-promo --apply
    python make_keys.py --reconcile    # sync ledger with D1 (status + import manual keys)
    python make_keys.py 500 --no-db-check         # skip the D1 dedupe (ledger-only; offline)

OUTPUTS (in ./out):
    keys-<batch>.csv / .sql   per-run files (.sql = what --apply runs)
    keys-master.csv           append-only ledger; --reconcile keeps it in sync with D1

Key format: XXXX-XXXX-XXXX-XXXX, alphabet ABCDEFGHJKLMNPQRSTUVWXYZ23456789
(no I/O/0/1 — matches codegnito_backend/src/lib/keyGen.ts, so keys activate).
"""
import argparse
import csv
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Windows consoles default to cp1252 and choke on non-ASCII — never crash on output.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors='backslashreplace')
    except Exception:
        pass

HERE         = Path(__file__).resolve().parent
ALPHABET     = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'   # 32 chars -> byte % 32 is unbiased
CALL_CAP     = 1500
PROD_ACCOUNT = 'd677090c77aaf56f7fa4ab28f355792e'
PROD_DB      = 'codegnito-backend-apac'
BACKEND_DIR  = HERE.parent / 'codegnito_backend'    # where wrangler is installed
COLS = ['key', 'duration_days', 'created_at', 'batch', 'status', 'shared_to',
        'shared_date', 'notes', 'db_used', 'db_activated_at', 'db_calls_used']


def parse_args():
    p = argparse.ArgumentParser(description='Generate / reconcile Codegnito activation keys.')
    p.add_argument('count', nargs='?', type=int, default=500,
                   help='number of keys to generate (default 500)')
    p.add_argument('--days', type=int, default=30, help='validity per key (default 30)')
    p.add_argument('--apply', action='store_true', help='also INSERT the keys into D1')
    p.add_argument('--local', action='store_true', help='use the local dev D1 instead of remote')
    p.add_argument('--reconcile', action='store_true', help='sync the ledger with D1 (no generation)')
    p.add_argument('--no-db-check', dest='no_db_check', action='store_true',
                   help='skip the D1 uniqueness query (dedupe vs the ledger only)')
    p.add_argument('--batch', default=None, help='batch label (default batch-<timestamp>)')
    p.add_argument('--db', default=PROD_DB)
    p.add_argument('--account', default=os.environ.get('CLOUDFLARE_ACCOUNT_ID', PROD_ACCOUNT))
    p.add_argument('--backend', default=str(BACKEND_DIR))
    return p.parse_args()


A      = parse_args()
OUT    = HERE / 'out'
MASTER = OUT / 'keys-master.csv'
REMOTE = '--local' if A.local else '--remote'


def gen_key():
    b = secrets.token_bytes(16)
    chars = [ALPHABET[b[i] % 32] for i in range(16)]
    return '-'.join(''.join(chars[i:i + 4]) for i in range(0, 16, 4))


def run_wrangler(args, capture):
    # shell=True needs npx on Windows but doesn't auto-quote — quote whitespace tokens.
    quoted = ' '.join(f'"{a}"' if ' ' in str(a) else str(a) for a in args)
    env = {**os.environ, 'CLOUDFLARE_ACCOUNT_ID': A.account}
    return subprocess.run(f'npx wrangler {quoted}', cwd=A.backend, env=env,
                          shell=True, capture_output=capture, text=True)


def fetch_db_rows():
    """All rows in activation_keys (the authoritative key universe).
    MUST use --command, not --file: `--file --remote` returns execution STATS,
    not the SELECT rows. Raises on failure (so we never generate blind)."""
    r = run_wrangler(['d1', 'execute', A.db, REMOTE, '-y', '--json', '--command',
                      'SELECT key, used, activated_at, ai_used, carry_credit, '
                      'duration_days, created_at, notes FROM activation_keys;'],
                     capture=True)
    if r.returncode != 0:
        raise RuntimeError('D1 query failed: ' + (r.stderr or r.stdout or '')[:300])
    clean = re.sub(r'\x1b\[[0-9;]*m', '', r.stdout or '')       # strip ANSI colour codes
    m = re.search(r'\[\s*\{.*\}\s*\]', clean, re.DOTALL)        # the [ {...} ] result block
    if not m:
        raise RuntimeError('D1 returned no parseable JSON')
    data  = json.loads(m.group(0))
    block = data[0] if isinstance(data, list) else data
    return block.get('results') or []


def load_ledger():
    if not MASTER.exists():
        return []
    with open(MASTER, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def write_ledger(rows):
    OUT.mkdir(parents=True, exist_ok=True)
    with open(MASTER, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, '') for c in COLS})


def iso(unix):
    if unix in (None, ''):
        return ''
    return datetime.fromtimestamp(int(unix), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ── RECONCILE: sync ledger with D1 (update status + import any DB-only keys) ──
if A.reconcile:
    rows = load_ledger()
    try:
        db_rows = fetch_db_rows()
    except Exception as e:
        print('ERROR: ' + str(e) + '\n  Check --db / --account / wrangler login.')
        sys.exit(1)
    in_ledger = {r['key'] for r in rows}
    db_map    = {d['key']: d for d in db_rows}
    used = unused = missing = shared = imported = 0

    for r in rows:                                   # update existing ledger rows
        if (r.get('status', '') or '').lower() == 'shared' or r.get('shared_to'):
            shared += 1
        d = db_map.get(r['key'])
        if not d:
            r['db_used'], r['db_activated_at'], r['db_calls_used'] = 'MISSING', '', ''
            missing += 1
            continue
        is_used = bool(d.get('used'))
        r['db_used'] = 'used' if is_used else 'unused'
        used += is_used; unused += (not is_used)
        r['db_activated_at'] = d.get('activated_at') or ''
        r['db_calls_used']   = '' if d.get('ai_used') is None else str(d['ai_used'])

    for d in db_rows:                                # import keys in D1 but not the ledger
        if d['key'] in in_ledger:
            continue
        is_used = bool(d.get('used'))
        rows.append({
            'key': d['key'], 'duration_days': d.get('duration_days', ''),
            'created_at': iso(d.get('created_at')), 'batch': d.get('notes') or '(manual)',
            'status': 'unshared', 'shared_to': '', 'shared_date': '', 'notes': '',
            'db_used': 'used' if is_used else 'unused', 'db_activated_at': d.get('activated_at') or '',
            'db_calls_used': '' if d.get('ai_used') is None else str(d['ai_used']),
        })
        imported += 1
        used += is_used; unused += (not is_used)

    write_ledger(rows)
    print(f'\nOK: Ledger synced with D1: {MASTER}')
    print(f'  total {len(rows)} |activated {used} |unused {unused} |'
          f'marked-shared {shared} |imported-from-DB {imported} |in-ledger-not-in-DB {missing}')
    sys.exit(0)


# ── GENERATE ──────────────────────────────────────────────────────────────────
if A.count < 1:
    print('ERROR: count must be a positive integer'); sys.exit(1)
if A.days < 1:
    print('ERROR: --days must be a positive integer'); sys.exit(1)

known    = {r['key'] for r in load_ledger()}
ledger_n = len(known)
db_n     = 0
if not A.no_db_check:
    try:
        db_rows = fetch_db_rows()
        db_n = len(db_rows)
        known.update(d['key'] for d in db_rows)
    except Exception as e:
        print('ERROR: Could not read existing keys from D1 for the uniqueness check:\n  ' + str(e))
        print('  Fix wrangler login / --account, OR re-run with --no-db-check (ledger-only).')
        sys.exit(1)
print(f'\nDedupe set: {len(known)} existing keys  '
      f'(D1 {db_n} + ledger {ledger_n}{" — DB check SKIPPED" if A.no_db_check else ""})')

fresh, seen = [], set(known)
while len(fresh) < A.count:
    k = gen_key()
    if k not in seen:
        seen.add(k); fresh.append(k)

stamp   = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
batch   = A.batch or f'batch-{stamp}'
now_s   = int(datetime.now(timezone.utc).timestamp())
now_iso = iso(now_s)
sql_esc = lambda s: str(s).replace("'", "''")

OUT.mkdir(parents=True, exist_ok=True)
sql_path = OUT / f'keys-{batch}.sql'
with open(sql_path, 'w', encoding='utf-8') as f:
    for k in fresh:
        f.write(f"INSERT OR IGNORE INTO activation_keys (key, duration_days, created_at, used, notes) "
                f"VALUES ('{k}', {A.days}, {now_s}, 0, '{sql_esc(batch)}');\n")

new_rows = [{'key': k, 'duration_days': A.days, 'created_at': now_iso, 'batch': batch,
             'status': 'unshared', 'shared_to': '', 'shared_date': '', 'notes': '',
             'db_used': '', 'db_activated_at': '', 'db_calls_used': ''} for k in fresh]

with open(OUT / f'keys-{batch}.csv', 'w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=COLS); w.writeheader()
    w.writerows(new_rows)

new_master = not MASTER.exists()
with open(MASTER, 'a', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=COLS)
    if new_master:
        w.writeheader()
    w.writerows(new_rows)

print(f'OK: Generated {len(fresh)} unique keys  ({A.days}-day |{CALL_CAP} calls each |batch "{batch}")')
print(f'  -SQL:    {sql_path}')
print(f'  -CSV:    {OUT / f"keys-{batch}.csv"}')
print(f'  -Ledger: {MASTER}  (appended)')

if not A.apply:
    print('\nReview the files. To insert into D1, re-run with --apply, or from PowerShell:')
    print(f'  cd "{A.backend}"; $env:CLOUDFLARE_ACCOUNT_ID="{A.account}"; '
          f'npx wrangler d1 execute {A.db} {REMOTE} -y --file="{sql_path}"')
    sys.exit(0)

print(f'\n-> Inserting into {"LOCAL" if A.local else "REMOTE"} D1 "{A.db}" (account {A.account})...\n')
r = run_wrangler(['d1', 'execute', A.db, REMOTE, '-y', '--file', str(sql_path)], capture=False)
if r.returncode == 0:
    print(f'\nOK: Inserted {len(fresh)} keys into {A.db}. Run --reconcile to refresh used/unused status.')
else:
    print(f'\nERROR: Insert failed (exit {r.returncode}). SQL saved at {sql_path} — fix and re-apply it.')
    sys.exit(r.returncode or 1)
