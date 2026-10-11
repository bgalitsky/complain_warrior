import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cw_export import build_export, from_sc100, KEYS


class ExportTests(unittest.TestCase):
    def test_schema_and_unicode(self):
        result = json.loads(build_export('case-1', {'consumer.full_name':'José García'}))
        self.assertEqual(result['schema'], 'cw.form-assistant.v1')
        self.assertEqual(result['values']['consumer.full_name'], 'José García')

    def test_sensitive_or_unknown_keys_rejected(self):
        with self.assertRaises(ValueError):
            build_export('1', {'access_token':'secret'})
        with self.assertRaises(ValueError):
            build_export('1', {'consumer.email':{'address':'x'}})

    def test_semantically_different_dates_and_ids_not_exported(self):
        data=from_sc100({'case_id':'internal-id','demand_date':'2026-01-01',
                         'defendant_legal_name':'Company','plaintiff_name':'Person'})
        self.assertNotIn('complaint.transaction_date', data)
        self.assertNotIn('complaint.reference', data)
        self.assertEqual(data['business.name'], 'Company')
        self.assertEqual(data['consumer.full_name'], 'Person')

    def test_python_js_schema_stay_in_sync(self):
        source=(Path(__file__).resolve().parents[1]/'chrome/engine.js').read_text()
        for key in KEYS:
            self.assertIn("'"+key+"'", source)


if __name__ == '__main__':
    unittest.main()
