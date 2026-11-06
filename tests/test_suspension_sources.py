import copy
import unittest
from unittest.mock import patch

from qmt.config import AppError
from qmt.research import DISCLOSURES, full_day_suspension


class SuspensionSourceTests(unittest.TestCase):
    def setUp(self):
        self.item=copy.deepcopy(DISCLOSURES['full_day_suspensions'][0])
        self.registry=patch.dict(DISCLOSURES,{'full_day_suspensions':[self.item]})
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def test_exact_dated_primary_proof_never_infers_other_dates_or_symbols(self):
        before=copy.deepcopy(self.item)
        proof=full_day_suspension('600566.SH','20250718')
        self.assertEqual(proof,{key:self.item[key] for key in
            ('symbol','day','full_day','source','source_sha256','pages','available_on')})
        self.assertEqual(proof['source'],'https://static.cninfo.com.cn/finalpage/2025-07-18/1224202264.PDF')
        self.assertIsNone(full_day_suspension('600566.SH','20250717'))
        self.assertIsNone(full_day_suspension('600566.SH','20250721'))
        self.assertIsNone(full_day_suspension('600567.SH','20250718'))
        self.assertEqual(self.item,before)

    def test_later_resumption_announcement_cannot_supply_earlier_proof(self):
        self.item['available_on']='20250721'
        self.assertIsNone(full_day_suspension('600566.SH','20250718'))
        self.assertIsNone(full_day_suspension('600566.SH','20250721'))

    def test_duplicate_or_non_full_day_registration_is_not_accepted(self):
        DISCLOSURES['full_day_suspensions'].append(copy.deepcopy(self.item))
        with self.assertRaisesRegex(AppError,'重复或冲突'):
            full_day_suspension('600566.SH','20250718')
        DISCLOSURES['full_day_suspensions'].pop()
        for value in (False,1,'true',None):
            self.item['full_day']=value
            with self.subTest(value=value),self.assertRaisesRegex(AppError,'全天停牌'):
                full_day_suspension('600566.SH','20250718')

    def test_source_and_identity_validation_fail_closed(self):
        original=copy.deepcopy(self.item)
        for change in ({'source':'http://example.org/file.pdf'},
                       {'source':'https://user:password@example.org/file.pdf'},
                       {'source_sha256':'f'*63},{'source_sha256':'g'*64},{'pages':''},
                       {'available_on':'20250230'}):
            self.item.clear();self.item.update({**original,**change})
            with self.subTest(change=change),self.assertRaises(AppError):
                full_day_suspension('600566.SH','20250718')
        for records in (None,{},['not a proof']):
            with self.subTest(records=records),patch.dict(DISCLOSURES,{'full_day_suspensions':records}),self.assertRaises(AppError):
                full_day_suspension('600566.SH','20250718')
        for code,day in (('bad','20250718'),('600566.SH','20250230')):
            with self.subTest(code=code,day=day),self.assertRaises(AppError):
                full_day_suspension(code,day)


if __name__=='__main__':
    unittest.main()
