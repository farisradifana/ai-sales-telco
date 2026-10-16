"""Extract Jawa Timur (kode 35) province/regency/district rows from cahyadsn/wilayah db/wilayah.sql.
The SQL file is read as DATA and never executed.
Usage: python -I scripts/extract_jatim.py <wilayah.sql> <out.csv> --commit <sha>"""
import argparse
import csv
import hashlib
import re
from pathlib import Path

ROW = re.compile(r"\('(35(?:\.\d+){0,2})','((?:[^'\\]|\\.|'')*)'\)")
LEVELS = {0: 'province', 1: 'regency', 2: 'district'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('out')
    ap.add_argument('--commit', required=True)
    a = ap.parse_args()
    raw = Path(a.src).read_bytes()
    rows = {}
    for kode, nama in ROW.findall(raw.decode('utf-8')):
        rows[kode] = nama.replace("\\'", "'").replace("''", "'").strip()
    out = sorted(rows.items(), key=lambda kv: [int(x) for x in kv[0].split('.')])
    counts = {name: sum(1 for k, _ in out if k.count('.') == i) for i, name in LEVELS.items()}
    assert counts == {'province': 1, 'regency': 38, 'district': 666}, counts
    for k, _ in out:
        if '.' in k:
            assert k.rsplit('.', 1)[0] in rows, 'orphan ' + k
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, lineterminator='\n')
        w.writerow(['kode', 'nama', 'level'])
        for k, n in out:
            w.writerow([k, n, LEVELS[k.count('.')]])
    print({'source_sha256': hashlib.sha256(raw).hexdigest(), 'commit': a.commit,
           'output_sha256': hashlib.sha256(Path(a.out).read_bytes()).hexdigest(), **counts})


if __name__ == '__main__':
    main()
