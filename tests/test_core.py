import csv
import json
import tempfile
import unittest
from pathlib import Path
from telco.generate import generate
from telco.validate_csv import validate
from telco.tools import recommend


class GeneratorTests(unittest.TestCase):
    def test_repeatability_and_seed_sensitivity(self):
        with tempfile.TemporaryDirectory() as d:
            a=generate(Path(d)/'a',42,100)
            b=generate(Path(d)/'b',42,100)
            c=generate(Path(d)/'c',43,100)
            self.assertEqual(a,b)
            self.assertNotEqual(a['files']['sell_out.csv']['sha256'],c['files']['sell_out.csv']['sha256'])
            self.assertEqual(validate(Path(d)/'a')['status'],'passed')

    def test_bad_count_and_existing_data(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError): generate(d,42,99)
            generate(d,42,5)
            with self.assertRaises(FileExistsError): generate(d,42,5)

    def test_corruption_detected(self):
        with tempfile.TemporaryDirectory() as d:
            generate(d,42,5)
            with (Path(d)/'sell_out.csv').open('a') as f: f.write('corrupted\n')
            with self.assertRaisesRegex(AssertionError,'sha256'): validate(d)

    def test_fk_error_detected_even_with_new_checksum(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            generate(d,42,5)
            p=Path(d)/'sell_out.csv'
            with p.open(newline='') as f: rows=list(csv.reader(f))
            rows[1][rows[0].index('product_id')]='9999'
            with p.open('w',newline='') as f: csv.writer(f).writerows(rows)
            mpath=Path(d)/'manifest.json'; m=json.loads(mpath.read_text())
            m['files']['sell_out.csv']['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
            mpath.write_text(json.dumps(m))
            with self.assertRaisesRegex(AssertionError,'keys/assignment'): validate(d)


class RecommendationTests(unittest.TestCase):
    def fixture(self):
        return {'current':['2026-09-01','2026-10-01'],'delta_idr':-100,
                'coverage':{'expected_outlet_days':30,'complete_outlet_days':30,'visits_done':0,'visits_planned':8},
                'baseline_coverage':{'expected_outlet_days':30,'complete_outlet_days':30},
                'watermark':'2026-09-30','drivers':[{'product_id':1,'delta_idr':-100}]}

    def test_decline_actions_link_evidence(self):
        r=recommend(self.fixture())
        self.assertTrue(r['data_ready'])
        self.assertEqual(len(r['actions']),2)
        self.assertIn('unproven',r['actions'][0]['confidence'])
        self.assertTrue(all(a['evidence'] for a in r['actions']))

    def test_missing_and_stale_data_gate(self):
        for field in ('watermark','coverage','baseline_coverage'):
            e=self.fixture()
            if field=='watermark': e[field]='2026-09-29'
            else: e[field]['complete_outlet_days']=29
            r=recommend(e)
            self.assertFalse(r['data_ready'])
            self.assertEqual(len(r['actions']),1)
            self.assertEqual(r['actions'][0]['confidence'],'insufficient_data')

    def test_zero_baseline_no_forced_decline(self):
        e=self.fixture();e['delta_idr']=0
        self.assertIn('tidak ada bukti',recommend(e)['actions'][0]['action'])


if __name__=='__main__': unittest.main()
