"""Deterministic CSV generator; Python stdlib only. No real PII or network."""
import argparse
import csv
import hashlib
import json
import random
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

START = date(2025, 1, 1)
END = date(2026, 9, 30)
TZ = timezone(timedelta(hours=7))
ROOT = Path(__file__).resolve().parents[1]
REGIONS = ROOT / 'data' / 'reference' / 'jatim_wilayah.csv'
# Pinned extract of Kepmendagri 300.2.2-2430/2025 via cahyadsn/wilayah@d68e8d5 (see data/reference/SOURCE.md).
REGIONS_SHA256 = '01f671e3d2c8bf969cb79baeffc5a93a4adc71f1c839a16c900bd8cbb579919d'
PRODUCTS = [
    (1, 'SIM-NOVA-8', 'Perdana Fiktif Nova 8GB 30 Hari', 'PERDANA', 'PCS', 25000, '25000'),
    (2, 'SIM-NOVA-18', 'Perdana Fiktif Nova 18GB 30 Hari', 'PERDANA', 'PCS', 50000, '50000'),
    (3, 'VCR-NOVA-3', 'Voucher Fiktif Nova 3GB 7 Hari', 'VOUCHER', 'PCS', 12000, '12000'),
    (4, 'VCR-NOVA-8', 'Voucher Fiktif Nova 8GB 30 Hari', 'VOUCHER', 'PCS', 22000, '22000'),
    (5, 'VCR-NOVA-20', 'Voucher Fiktif Nova 20GB 30 Hari', 'VOUCHER', 'PCS', 45000, '45000'),
    (6, 'BAL-NOVA-IDR', 'Saldo Fiktif Nova Rupiah', 'SALDO', 'IDR', 1, '1.0100'),
]


def load_regions(path=None):
    path = Path(path or REGIONS)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != REGIONS_SHA256:
        raise ValueError('Region reference checksum mismatch: ' + str(path))
    rows = list(csv.DictReader(raw.decode('utf-8').splitlines()))
    prov = [(r['kode'], r['nama']) for r in rows if r['level'] == 'province']
    regs = [(r['kode'], r['nama']) for r in rows if r['level'] == 'regency']
    dists = [(r['kode'], r['nama']) for r in rows if r['level'] == 'district']
    assert len(prov) == 1 and len(regs) == 38 and len(dists) == 666
    return prov[0], regs, dists


def place_outlets(by_reg, n_outlets):
    """Spread outlets over every regency (first regencies get one extra), each outlet in a distinct district chosen
    evenly across the regency's code-ordered district list. Deterministic; does not consume the transaction RNG."""
    regs = sorted(by_reg)
    base, extra = divmod(n_outlets, len(regs))
    out = []
    for i, rid in enumerate(regs):
        n = base + (1 if i < extra else 0)
        ds = by_reg[rid]
        out.extend(ds[(k * len(ds)) // n] for k in range(n))
    return out


def generate(dest, seed=42, rows=500_000, regions=None):
    if rows <= 0 or rows % 5:
        raise ValueError('rows must be positive and divisible by 5 (1 sell-in : 4 sell-out)')
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.glob('*.csv')) or (dest / 'manifest.json').exists():
        raise FileExistsError('Use an empty data directory; existing dataset is preserved')
    rng = random.Random(seed)
    files = {}
    writers = {}
    counts = Counter()

    def table(name, header):
        f = (dest / (name + '.csv')).open('w', encoding='utf-8', newline='')
        files[name] = f
        writers[name] = csv.writer(f, lineterminator='\n')
        writers[name].writerow(header.split(','))

    def emit(name, row):
        writers[name].writerow(row)
        counts[name] += 1

    headers = {
        'province': 'id,code,name',
        'regency': 'id,province_id,code,name',
        'district': 'id,regency_id,code,name',
        'sales': 'id,code,name,district_id',
        'outlet': 'id,code,name,district_id,owner_alias,fictional_address,latitude,longitude,opened_on,is_synthetic',
        'product': 'id,sku,name,category,stock_unit,face_value_idr,list_price',
        'assignment': 'id,outlet_id,sales_id,valid_from,valid_to',
        'stock_account': 'id,sales_id,outlet_id',
        'supply': 'id,external_key,occurred_at,account_id,product_id,quantity,reason',
        'sell_in': 'id,external_key,occurred_at,outlet_id,sales_id,assignment_id,product_id,quantity,unit_price,discount_idr',
        'sell_out': 'id,external_key,occurred_at,outlet_id,sales_id,assignment_id,product_id,quantity,unit_price,discount_idr,customer_token',
        'sales_target': 'sales_id,target_date,channel,revenue_idr,rationale',
        'outlet_day': 'outlet_id,day,expected_open,sell_out_complete,sell_in_complete,visits_planned,visits_done,observed_stockout_minutes,observation_source',
        'ingestion_watermark': 'source,complete_through,batch_key',
    }
    for name, header in headers.items():
        table(name, header)
    try:
        prov, regs, dists = load_regions(regions)
        emit('province', (1, prov[0], prov[1]))
        reg_id = {}
        for rid, (kode, nama) in enumerate(regs, 1):
            reg_id[kode] = rid
            emit('regency', (rid, 1, kode, nama))
        by_reg = {}
        for did, (kode, nama) in enumerate(dists, 1):
            rid = reg_id[kode.rsplit('.', 1)[0]]
            by_reg.setdefault(rid, []).append(did)
            emit('district', (did, rid, kode, nama))
        outlet_district = place_outlets(by_reg, 120)
        for s in range(1, 13):
            # Base district = district of the sales' first outlet; sales covers outlets (s-1)*10+1..s*10 as in v1.
            emit('sales', (s, f'SLS-{s:03}', f'Sales Fiktif {s:03}', outlet_district[(s-1)*10]))
            emit('stock_account', (s, s, ''))
        for o in range(1, 121):
            d = outlet_district[o-1]
            s = (o-1)//10+1
            # Real administrative district, fictional outlet: no coordinates so no real location is implied.
            emit('outlet', (o, f'OUT-{o:04}', f'Outlet Fiktif {o:04}', d, f'Pemilik-SYN-{o:04}',
                            f'Alamat Fiktif No {o:04} (BUKAN ALAMAT NYATA)', '', '', START, 'true'))
            emit('assignment', (o, o, s, START, '2027-01-01'))
            emit('stock_account', (1000+o, '', o))
        for p in PRODUCTS:
            emit('product', p)
        days = (END-START).days+1
        for offset in range(days):
            day = START + timedelta(days=offset)
            for s in range(1, 13):
                # Provisional planning assumptions, never fitted to generated outcomes.
                for channel, target in [('sell_in', 3500000), ('sell_out', 4000000)]:
                    emit('sales_target', (s, day, channel, target, 'Fictional planning target; validate with business owner'))
            for o in range(1, 121):
                planned = int(day.weekday() in (0, 3))
                missed = o <= 10 and day >= date(2026, 7, 1)
                emit('outlet_day', (o, day, 'true', 'true', 'true', planned, 0 if missed else planned, '', 'SYNTHETIC_VISIT_LOG; stockout not measured'))
        # 100,000 bundles, each one sell-in + four sell-out product lines.
        # Supply/stock events excluded from the 500,000 commercial-line requirement.
        bundles = []
        for i in range(1, rows//5+1):
            day = START + timedelta(days=rng.randrange(days))
            # Seasonality in quantity; reduced selection frequency of district 1 after July 2026.
            o = rng.randint(1, 120)
            if day >= date(2026, 7, 1) and o <= 10 and rng.random() < .82:
                o = rng.randint(11, 120)
            p = rng.choices(range(1, 7), weights=[17, 10, 25, 22, 14, 12])[0]
            qs = [rng.randint(1, 4) + int(day.month in (3, 12)) for _ in range(4)]
            if p == 6:
                qs = [q*10000 for q in qs]
            bundles.append((day, i, o, p, qs))
        bundles.sort(key=lambda b: (b[0], b[1]))
        for day, i, o, p, qs in bundles:
            s = (o-1)//10+1
            timestamp = lambda hour, second=0: datetime.combine(day, time(hour, 0, second), TZ).isoformat()
            total = sum(qs)
            price = PRODUCTS[p-1][6]
            # Price/discount scenario is observational; visits are not assumed causal.
            disc_pct = 10 if day >= date(2026, 7, 1) and o <= 10 and p != 6 else 0
            sell_in_price = '1.0000' if p == 6 else str(int(price)*9//10)
            emit('supply', (i, f'SYN-SUP-{i}', timestamp(7), s, p, total, 'Synthetic external replenishment'))
            emit('sell_in', (i, f'SYN-IN-{i}', timestamp(8), o, s, o, p, total, sell_in_price, 0))
            for j, q in enumerate(qs):
                tx_id = (i-1)*4+j+1
                discount = q*int(price)*disc_pct//100 if p != 6 else 0
                emit('sell_out', (tx_id, f'SYN-OUT-{tx_id}', timestamp(10, j), o, s, o, p, q, price, discount, f'SYN-CUSTOMER-{tx_id:07}'))
        for channel in ('sell_in', 'sell_out'):
            emit('ingestion_watermark', (channel, END, f'SYN-{seed}-{channel}'))
    finally:
        for f in files.values():
            f.close()
    manifest = {'generator_version': '2.0.0', 'regions': {'source': 'cahyadsn/wilayah@d68e8d5 (Kepmendagri 300.2.2-2430/2025)', 'sha256': REGIONS_SHA256, 'scope': 'Provinsi Jawa Timur: 38 kabupaten/kota, 666 kecamatan'}, 'seed': seed, 'rows': rows, 'sell_in_rows': rows//5,
                'sell_out_rows': rows*4//5, 'period': [str(START), str(END)], 'timezone': 'Asia/Jakarta',
                'python_rng': 'random.Random / CPython 3.12 (pin for byte reproducibility)',
                'grain': 'One completed product line per transaction ID',
                'counts': dict(counts), 'files': {}}
    for name in sorted(files):
        path = dest / (name+'.csv')
        manifest['files'][path.name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}
    (dest/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    (dest/'scenario_truth.json').write_text(json.dumps({
        'synthetic_only': True, 'excluded_from_agent_evidence': True,
        'scenario': 'Outlets OUT-0001..OUT-0010 (sales SLS-001) reduced bundle probability from July 2026; physical discount 10%; fewer recorded visits.',
        'no_causal_claim': 'Co-occurrence was designed; not evidence that missed visits cause lower demand.',
        'stock': 'Just-in-time exact receipts, no measured stockout, no customers lost dataset.',
        'limitations': 'Fixed four sell-out lines per receipt; daily zero final balances; no churn, baskets, real competition, taxes or COGS.'
    }, indent=2)+'\n', encoding='utf-8')
    return manifest


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', default='data/full')
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--rows', type=int, default=500000)
    a = ap.parse_args()
    print(json.dumps(generate(a.out, a.seed, a.rows), indent=2))
