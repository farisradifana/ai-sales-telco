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
SALES_AREA = ROOT / 'data' / 'reference' / 'jatim_sales_area.csv'
TARGET_PER_OUTLET = {'sell_in': 350000, 'sell_out': 400000}  # fictional planning parameters per assigned outlet/day
ANGKA = ['', 'Satu', 'Dua', 'Tiga', 'Empat', 'Lima', 'Enam', 'Tujuh', 'Delapan', 'Sembilan', 'Sepuluh', 'Sebelas']
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


def angka(n):
    if n < 12:
        return ANGKA[n]
    if n < 20:
        return ANGKA[n - 10] + ' Belas'
    tens, ones = divmod(n, 10)
    return (ANGKA[tens] + ' Puluh' + (' ' + ANGKA[ones] if ones else '')).replace('Satu Puluh', 'Sepuluh')


def short_regency(name):
    # 'Kabupaten Madiun' -> 'Madiun'; 'Kota Madiun' stays 'Kota Madiun' (avoids collision with the regency).
    return name[len('Kabupaten '):] if name.startswith('Kabupaten ') else name


def build_org(regs, dists, reg_id, by_reg, areas):
    """Positions: 1 head of region, 2 heads of sales (EAST/WEST by data/reference/jatim_sales_area.csv), 1 head of branch
    per regency, territory managers for each run of 3 districts (code order) inside a regency, 1 sales per district.
    Persons are fictional role holders; ids/codes are deterministic."""
    positions, territory = [], []
    dist_name = {i + 1: n for i, (_, n) in enumerate(dists)}
    dist_code = {i + 1: k for i, (k, _) in enumerate(dists)}
    reg_name = {reg_id[k]: n for k, n in regs}
    dup = Counter(dist_name.values())
    positions.append((1, 'HOR-35', 'head_of_region', 'Head of Region Jawa Timur', '', '', ''))
    territory.append((1, 'province', 1))
    hos = {'WEST': 2, 'EAST': 3}
    for area, pid in hos.items():
        positions.append((pid, f'HOS-{area}', 'head_of_sales', f'Head of Sales {area.title()}', 1, area, ''))
    nid = 4
    for kode, nama in regs:
        rid = reg_id[kode]
        hob = nid
        nid += 1
        positions.append((hob, f'HOB-{kode}', 'head_of_branch', f'Head of Branch {short_regency(nama)}', hos[areas[kode]], areas[kode], ''))
        territory.append((hob, 'regency', rid))
        territory.append((hos[areas[kode]], 'regency', rid))
        ds = by_reg[rid]
        for k in range(0, len(ds), 3):
            tm = nid
            nid += 1
            positions.append((tm, f'TM-{kode}-{k // 3 + 1:02}', 'territory_manager',
                              f'Territory Manager {short_regency(nama)} {angka(k // 3 + 1)}', hob, areas[kode], ''))
            for did in ds[k:k + 3]:
                territory.append((tm, 'district', did))
                sname = dist_name[did].replace(' ', '')
                if dup[dist_name[did]] > 1:
                    sname += '_' + short_regency(reg_name[rid]).replace(' ', '')
                positions.append((nid, f'SLS-{dist_code[did]}', 'sales', f'Sales_{sname}', tm, areas[kode], did))
                territory.append((nid, 'district', did))
                nid += 1
    return positions, territory


def load_sales_area(path=None):
    rows = list(csv.DictReader(Path(path or SALES_AREA).read_text(encoding='utf-8').splitlines()))
    areas = {r['regency_kode']: r['sales_area'] for r in rows}
    assert len(areas) == 38 and Counter(areas.values()) == {'EAST': 19, 'WEST': 19}, Counter(areas.values())
    return areas


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
        'org_position': 'id,code,level,title,parent_id,sales_area,sales_id',
        'org_territory': 'position_id,scope_type,entity_id',
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
        positions, territory = build_org(regs, dists, reg_id, by_reg, load_sales_area())
        # One sales person per district (sales.id = district.id); each holds a sales stock account.
        for pos in positions:
            if pos[2] == 'sales':
                emit('sales', (pos[6], pos[1], pos[3], pos[6]))
                emit('stock_account', (pos[6], pos[6], ''))
        for o in range(1, 121):
            d = outlet_district[o-1]
            s = d  # outlet is covered by the sales of its district
            # Real administrative district, fictional outlet: no coordinates so no real location is implied.
            emit('outlet', (o, f'OUT-{o:04}', f'Outlet Fiktif {o:04}', d, f'Pemilik-SYN-{o:04}',
                            f'Alamat Fiktif No {o:04} (BUKAN ALAMAT NYATA)', '', '', START, 'true'))
            emit('assignment', (o, o, s, START, '2027-01-01'))
            emit('stock_account', (1000+o, '', o))
        for pos in positions:
            emit('org_position', pos)
        for t in territory:
            emit('org_territory', t)
        sales_with_outlets = Counter(outlet_district)
        for p in PRODUCTS:
            emit('product', p)
        days = (END-START).days+1
        for offset in range(days):
            day = START + timedelta(days=offset)
            for s in sales_with_outlets:
                # Provisional planning assumptions per assigned outlet, never fitted to generated outcomes.
                for channel, per in TARGET_PER_OUTLET.items():
                    emit('sales_target', (s, day, channel, per * sales_with_outlets[s],
                                          'Fictional planning target per assigned outlet; validate with business owner'))
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
            s = outlet_district[o-1]
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
    manifest = {'generator_version': '3.0.0', 'organisation': {'sales_area_reference': 'data/reference/jatim_sales_area.csv',
                'levels': dict(Counter(p[2] for p in positions))}, 'regions': {'source': 'cahyadsn/wilayah@d68e8d5 (Kepmendagri 300.2.2-2430/2025)', 'sha256': REGIONS_SHA256, 'scope': 'Provinsi Jawa Timur: 38 kabupaten/kota, 666 kecamatan'}, 'seed': seed, 'rows': rows, 'sell_in_rows': rows//5,
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
        'scenario': 'Outlets OUT-0001..OUT-0010 (each covered by its district sales) reduced bundle probability from July 2026; physical discount 10%; fewer recorded visits.',
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
