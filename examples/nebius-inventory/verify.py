"""Independent acceptance tests: kept outside the model's task workspace."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
import sys

WS=Path(sys.argv.pop(1)).resolve()
spec=importlib.util.spec_from_file_location('generated_inventory',WS/'inventory.py')
inventory=importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)

class IndependentAcceptance(unittest.TestCase):
    def row(self,**kwargs):
        return dict(sku='X',on_hand=10,reserved=3,inbound=2,reorder_point=10,**kwargs)
    def test_integer_input(self):
        self.assertEqual(inventory.plan_inventory([self.row()]),[dict(sku='X',available=7,projected=9,reorder=1)])
    def test_csv_string_input(self):
        r={k:str(v) for k,v in self.row().items()}
        self.assertEqual(inventory.plan_inventory([r])[0]['reorder'],1)
    def test_empty(self):
        self.assertEqual(inventory.plan_inventory([]),[])
    def test_invalid_values(self):
        for field in ('on_hand','reserved','inbound','reorder_point'):
            for invalid in (-1,'-1',1.5,'1.5'):
                with self.subTest(field=field,value=invalid):
                    r=self.row();r[field]=invalid
                    with self.assertRaises(ValueError): inventory.plan_inventory([r])
    def test_overreserved(self):
        r=self.row();r['reserved']=11
        with self.assertRaises(ValueError): inventory.plan_inventory([r])
    def test_duplicate(self):
        with self.assertRaises(ValueError): inventory.plan_inventory([self.row(),self.row()])
    def test_cli_result(self):
        subprocess.run(['python3','inventory.py','inventory.csv'],cwd=WS,check=True,capture_output=True)
        self.assertEqual(json.loads((WS/'result.json').read_text()),[
            dict(sku='A',available=7,projected=9,reorder=1),
            dict(sku='B',available=4,projected=14,reorder=0),
            dict(sku='C',available=0,projected=0,reorder=4)])
    def test_deliverables_present(self):
        for name in ('inventory.py','test_inventory.py','result.json','REPORT.md'):
            self.assertTrue((WS/name).is_file(),name)

if __name__=='__main__': unittest.main(verbosity=2)
