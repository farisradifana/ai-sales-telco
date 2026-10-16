"""Live WhatsApp setup helper. Secrets/phone numbers stay in .local/live.env and the encrypted identity table; nothing
is printed except masked values and blind-index HMACs.

  python scripts/wa_live_setup.py init                       # create .local/live.env template + random verify token
  python scripts/wa_live_setup.py check                      # read-only Graph API check of the AI-agent number (no send)
  python scripts/wa_live_setup.py register-number            # activate the number for Cloud API (owner types 2FA PIN)
  python scripts/wa_live_setup.py subscribe                  # subscribe this app to the WABA (webhook delivery)
  python scripts/wa_live_setup.py register --principal sls-35.19.01
        # asks the recipient number with a hidden prompt, stores it encrypted, adds its HMAC to the live allowlist

Works from PowerShell or bash; local keys are read from .local/dev.env (override with IDENTITY_DATABASE_URL).
"""
import argparse
import getpass
import os
import re
import secrets
import sys
from pathlib import Path

LIVE = Path('.local/live.env')
KEYS = ['WA_PHONE_NUMBER_ID', 'WA_WABA_ID', 'WA_ACCESS_TOKEN', 'WA_APP_SECRET', 'WA_VERIFY_TOKEN', 'WA_LIVE_ALLOWLIST_HMACS',
        'WA_GRAPH_VERSION', 'ROUTER_BASE_URL', 'ROUTER_API_KEY', 'MODEL_ID', 'FALLBACK_MODEL_ID']
COMMENTS = {  # comment lines written above a key
    'WA_PHONE_NUMBER_ID': ['# --- WhatsApp Cloud API (Meta) ---'],
    'ROUTER_BASE_URL': ['# --- Model AI via 9Router (provider berizin dengan API key, BUKAN login akun langganan) ---',
                        '# ROUTER_BASE_URL : alamat 9Router lokal, tanpa /v1 (default http://127.0.0.1:20128)',
                        '# ROUTER_API_KEY  : API key klien yang dibuat di dashboard 9Router (kosongkan bila tidak diminta)',
                        '# MODEL_ID        : nama model PERSIS seperti tampil di 9Router',
                        '# FALLBACK_MODEL_ID (opsional): model cadangan di 9Router bila model utama gagal'],
}
DEFAULTS = {'WA_GRAPH_VERSION': 'v23.0', 'ROUTER_BASE_URL': 'http://127.0.0.1:20128'}


def read() -> dict:
    out = {}
    if LIVE.exists():
        for line in LIVE.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip()
    return out


def write(vals: dict) -> None:
    LIVE.parent.mkdir(exist_ok=True)
    lines = ['# Live WhatsApp Cloud API config (gitignored). Never paste these values into chat or commits.']
    for k in KEYS:
        if k in COMMENTS:
            lines.extend(COMMENTS[k])
        lines.append(f'{k}={vals.get(k) or DEFAULTS.get(k, "")}')
    # Keep any extra keys the owner added by hand.
    lines += [f'{k}={v}' for k, v in vals.items() if k not in KEYS]
    LIVE.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def mask(v: str) -> str:
    return (v[:3] + '…' + v[-2:]) if len(v) > 6 else '***'


def cmd_init(_):
    vals = read()
    vals.setdefault('WA_GRAPH_VERSION', 'v23.0')
    if not vals.get('WA_VERIFY_TOKEN'):
        vals['WA_VERIFY_TOKEN'] = secrets.token_urlsafe(24)
    write(vals)
    missing = [k for k in ('WA_PHONE_NUMBER_ID', 'WA_WABA_ID', 'WA_ACCESS_TOKEN', 'WA_APP_SECRET') if not vals.get(k)]
    print(f'{LIVE} siap. Verify token sudah dibuat (salin dari file ke Meta). Belum diisi: {", ".join(missing) or "-"}')


def cmd_check(_):
    import httpx
    v = read()
    for k in ('WA_PHONE_NUMBER_ID', 'WA_ACCESS_TOKEN'):
        if not v.get(k):
            sys.exit(f'{k} belum diisi di {LIVE}')
    url = f"https://graph.facebook.com/{v.get('WA_GRAPH_VERSION') or 'v23.0'}/{v['WA_PHONE_NUMBER_ID']}"
    r = httpx.get(url, params={'fields': 'display_phone_number,verified_name,quality_rating,code_verification_status,platform_type'},
                  headers={'Authorization': f"Bearer {v['WA_ACCESS_TOKEN']}"}, timeout=20)
    if r.status_code != 200:
        err = r.json().get('error', {})
        sys.exit(f"Graph API {r.status_code}: {err.get('type')} {err.get('code')} — {err.get('message', '')[:160]}")
    d = r.json()
    num = d.get('display_phone_number', '')
    print({'display_phone_number': num[:4] + '…' + num[-3:] if num else None, 'verified_name': d.get('verified_name'),
           'quality_rating': d.get('quality_rating'), 'code_verification_status': d.get('code_verification_status'),
           'platform_type': d.get('platform_type')})


def _graph(method, path, v, **kw):
    import httpx
    url = f"https://graph.facebook.com/{v.get('WA_GRAPH_VERSION') or 'v23.0'}/{path}"
    r = httpx.request(method, url, headers={'Authorization': f"Bearer {v['WA_ACCESS_TOKEN']}"}, timeout=30, **kw)
    if r.status_code >= 400:
        err = r.json().get('error', {})
        sys.exit(f"Graph API {r.status_code}: {err.get('code')} — {err.get('message', '')[:200]}")
    return r.json()


def cmd_register_number(_):
    """Activate the number for Cloud API. The 6-digit two-step PIN is typed by the account owner (hidden), never stored."""
    v = read()
    pin = getpass.getpass('Tetapkan PIN verifikasi dua langkah (6 digit, tidak ditampilkan): ').strip()
    if not re.fullmatch(r'\d{6}', pin):
        sys.exit('PIN harus 6 digit angka.')
    if getpass.getpass('Ulangi PIN: ').strip() != pin:
        sys.exit('PIN tidak sama.')
    print(_graph('POST', f"{v['WA_PHONE_NUMBER_ID']}/register", v, json={'messaging_product': 'whatsapp', 'pin': pin}))
    print('Simpan PIN ini di password manager Anda; dibutuhkan bila nomor didaftarkan ulang.')


def cmd_subscribe(_):
    v = read()
    if not v.get('WA_WABA_ID'):
        sys.exit('WA_WABA_ID belum diisi.')
    print(_graph('POST', f"{v['WA_WABA_ID']}/subscribed_apps", v))
    apps = _graph('GET', f"{v['WA_WABA_ID']}/subscribed_apps", v)
    print('subscribed apps:', [a.get('whatsapp_business_api_data', {}).get('name') for a in apps.get('data', [])])


def _load_local_env(db: str) -> None:
    """Make the command work from plain PowerShell: read .local/dev.env (keys) and build the identity DB URL for the
    local disposable cluster unless the caller already exported IDENTITY_DATABASE_URL."""
    dev = Path('.local/dev.env')
    if dev.exists():
        for line in dev.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.startswith('#'):
                k, val = line.split('=', 1)
                os.environ.setdefault(k.strip(), val.strip())
    os.environ.setdefault('READER_DATABASE_URL', 'postgresql://unused')
    os.environ.setdefault('WRITER_DATABASE_URL', 'postgresql://unused')
    if not os.environ.get('IDENTITY_DATABASE_URL'):
        pw = os.environ['TELCO_API_IDENTITY_PASSWORD']
        os.environ['IDENTITY_DATABASE_URL'] = f'postgresql://telco_api_identity:{pw}@127.0.0.1:55432/{db}'


def cmd_register(a):
    sys.path.insert(0, '.')
    import psycopg
    from telco_api.identity import IdentityVault
    from telco_api.settings import Settings
    _load_local_env(a.db)
    raw = getpass.getpass('Nomor WhatsApp penerima uji (format internasional tanpa +, mis. 62812...): ').strip()
    if not re.fullmatch(r'[1-9]\d{7,14}', raw):
        sys.exit('Format nomor tidak valid (harus E.164 tanpa +).')
    s = Settings()
    vault = IdentityVault(s)
    with psycopg.connect(os.environ['IDENTITY_DATABASE_URL']) as c:
        if not c.execute('SELECT 1 FROM telco.app_principal WHERE principal_id=%s AND active', (a.principal,)).fetchone():
            sys.exit('Principal tidak ditemukan/aktif.')
        vault.register(c, raw, a.principal)
    h = vault.blind_index(raw)
    vals = read()
    allow = {x for x in vals.get('WA_LIVE_ALLOWLIST_HMACS', '').split(',') if x}
    allow.add(h)
    vals['WA_LIVE_ALLOWLIST_HMACS'] = ','.join(sorted(allow))
    write(vals)
    print(f'Terdaftar untuk {a.principal}; allowlist berisi {len(allow)} penerima. HMAC {h[:12]}… (nomor tidak ditampilkan).')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('init').set_defaults(fn=cmd_init)
    sub.add_parser('check').set_defaults(fn=cmd_check)
    sub.add_parser('register-number').set_defaults(fn=cmd_register_number)
    sub.add_parser('subscribe').set_defaults(fn=cmd_subscribe)
    r = sub.add_parser('register')
    r.add_argument('--principal', required=True)
    r.add_argument('--db', default='telco_test_dev3', help='local disposable DB (ignored if IDENTITY_DATABASE_URL is set)')
    r.set_defaults(fn=cmd_register)
    a = ap.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
