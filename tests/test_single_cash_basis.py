import copy
import json
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from qmt.config import AppError, load_config
from qmt.ledger import Ledger
from qmt.research import DISCLOSURES, annual_cash_decision, annual_dividend_cash, implemented_dividends


PROOF = copy.deepcopy(DISCLOSURES['cash_basis'][0])
LEGACY_KEYS = {('600578.SH', '20120630'), ('000726.SZ', '20121231'), ('600578.SH', '20130630')}
PROOFS = [copy.deepcopy(p) for p in DISCLOSURES['cash_basis']
          if (p['expected']['ts_code'], p['expected']['end_date']) in LEGACY_KEYS]
RAW = PROOF['expected']
DAY = '20130411'
CODE = '600578.SH'
BALANCES = {'20121231': {'total_share': '1947388801'}}


class SingleCashBasisTests(unittest.TestCase):
    def test_late_proof_changes_company_amount_not_event_or_fiscal_period(self):
        expected = {
            ('600578.SH', '20120630'): ('20130411', '1947388801', '486847200.25', '196806387.00'),
            ('000726.SZ', '20121231'): ('20150404', '1008894800', '282490544.00', '268816098.88'),
            ('600578.SH', '20130630'): ('20150402', '4617320954', '461732095.40', '230866047.70'),
        }
        self.assertEqual({(p['expected']['ts_code'], p['expected']['end_date']) for p in PROOFS}, set(expected))
        for proof in PROOFS:
            raw = copy.deepcopy(proof['expected'])
            day, year_end_shares, old_cash, new_cash = expected[raw['ts_code'], raw['end_date']]
            before_day = (datetime.strptime(day, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')
            period = raw['end_date'][:4] + '1231'
            with self.subTest(symbol=raw['ts_code'], event_period=raw['end_date']):
                self.assertEqual(proof['available_on'], day)
                before = implemented_dividends([raw], before_day)
                after = implemented_dividends([raw], day)
                self.assertEqual(before, [proof['expected']])
                self.assertEqual(raw, proof['expected'])
                self.assertEqual(len(after), 1)
                for key, value in raw.items():
                    self.assertEqual(json.dumps(after[0][key]), json.dumps(value), key)
                self.assertEqual(after[0]['correction_available_on'], day)
                self.assertGreater(day, raw['pay_date'])
                self.assertEqual(after[0]['correction_originals'], [raw])
                self.assertNotIn('stock_list_date', after[0])
                component = after[0]['fiscal_components'][0]
                self.assertEqual(component['end_date'], raw['end_date'])
                self.assertNotIn('ordinary_cash_div_tax', component)
                self.assertNotIn('special_cash_div_tax', component)
                # Hold the supplied balance constant to isolate aggregation,
                # without claiming earlier financial-source availability.
                balances = {period: {'total_share': year_end_shares}}
                old = annual_dividend_cash(before, balances)
                new = annual_dividend_cash(after, balances)
                self.assertEqual(set(old), {period})
                self.assertEqual(set(new), {period})
                self.assertEqual(Decimal(old[period]['estimated_dividend_cash']), Decimal(old_cash))
                self.assertEqual(Decimal(new[period]['estimated_dividend_cash']), Decimal(new_cash))
                for field in ('implemented_dps', 'ordinary_dps'):
                    self.assertEqual(old[period][field], new[period][field])
                self.assertEqual(new[period]['dividend_cash_basis'][0]['basis'], 'verified_distribution_shares')
                self.assertEqual(annual_dividend_cash(after, {}, f'{int(period[:4])+1}0101'), {})

    def test_canonical_is_idempotent_but_unavailable_before_its_proof(self):
        for proof in PROOFS:
            day = proof['available_on']
            before = (datetime.strptime(day, '%Y%m%d') - timedelta(days=1)).strftime('%Y%m%d')
            with self.subTest(event=proof['expected']):
                events = implemented_dividends([proof['expected']], day)
                saved = copy.deepcopy(events)
                self.assertEqual(implemented_dividends(events, day), events)
                self.assertEqual(implemented_dividends(events, '20150529'), events)
                self.assertEqual(events, saved)
                with self.assertRaises(AppError):
                    implemented_dividends(events, before)

    def test_jingneng_2013_two_share_bases_preserve_both_original_dps(self):
        interim = next(p['expected'] for p in PROOFS if p['expected']['end_date'] == '20130630')
        final = {'ts_code': CODE, 'end_date': '20131231', 'ann_date': '20140621',
                 'div_proc': '实施', 'cash_div_tax': .2, 'stk_div': 0.0,
                 'record_date': '20140818', 'ex_date': '20140819',
                 'pay_date': '20140819', 'imp_ann_date': '20140813'}
        balances = {'20131231': {'total_share': '4617320954'}}
        before = annual_dividend_cash(implemented_dividends([interim, final], '20150401'), balances)['20131231']
        events = implemented_dividends([interim, final], '20150402')
        after = annual_dividend_cash(events, balances)['20131231']
        self.assertEqual([e['cash_div_tax'] for e in events], [.1, .2])
        self.assertEqual(after['implemented_dps'], '0.3')
        self.assertEqual(Decimal(before['estimated_dividend_cash']), Decimal('1385196286.20'))
        self.assertEqual(Decimal(after['estimated_dividend_cash']), Decimal('1154330238.50'))

    def test_interim_completes_fiscal_year_only_on_independent_annual_proof_day(self):
        annual_raw = PROOF['annual_composition']['expected']
        rows = copy.deepcopy([RAW, annual_raw])
        before = annual_cash_decision(rows, CODE, '20121231', '20130503', BALANCES)
        after = annual_cash_decision(rows, CODE, '20121231', '20130504', BALANCES)
        self.assertEqual(before['stage'], 'unresolved')
        self.assertEqual(before['cash_scope'], 'incomplete_implemented_year')
        self.assertEqual(after['stage'], 'implemented')
        self.assertEqual(after['available_on'], '20130504')
        self.assertEqual(Decimal(after['total_cash']), Decimal('196806387.00'))
        self.assertEqual(after['annual_original'], annual_raw)
        self.assertNotIn('annual_originals', after)
        self.assertIsNone(after['annual_original']['cash_div_tax'])
        self.assertEqual(rows, [RAW, annual_raw])
        forged = copy.deepcopy(PROOF)
        forged['annual_composition']['total_cash'] = '196806388.00'
        with patch.dict(DISCLOSURES, {'cash_basis': [forged]}), self.assertRaises(AppError):
            annual_cash_decision(rows, CODE, '20121231', '20130504', BALANCES)

    def test_complete_raw_values_types_and_duplicate_revisions_must_match(self):
        changes = [{'cash_div_tax': '0.25'}, {'stk_div': 0}, {'cash_div_tax': .26},
                   {'record_date': '20120919'}, {'ex_date': '20120922'},
                   {'pay_date': '20120928'}, {'end_date': '20121231'},
                   {'extra_source_field': 'unknown'}]
        variants = [[{**RAW, **change}] for change in changes]
        variants += [[{k: v for k, v in RAW.items() if k != 'stk_div'}],
                     [RAW, copy.deepcopy(RAW)], [RAW, {**RAW, 'cash_div_tax': .26}]]
        canonical = implemented_dividends([RAW], DAY)[0]
        variants += [[canonical, RAW], [canonical, copy.deepcopy(canonical)]]
        for rows in variants:
            with self.subTest(rows=rows), self.assertRaises(AppError):
                implemented_dividends(rows, DAY)

    def test_unknown_future_revision_blocks_only_when_disclosed(self):
        future = {**RAW, 'record_date': '20120919', 'cash_div_tax': .26,
                  'ann_date': '20130506', 'imp_ann_date': '20130506'}
        canonical = implemented_dividends([RAW], DAY)
        for rows in ([RAW], canonical):
            with self.subTest(canonical=rows == canonical):
                self.assertEqual(implemented_dividends(rows + [future], DAY), canonical)
                with self.assertRaises(AppError):
                    implemented_dividends(rows + [future], '20130506')

    def test_forged_canonical_fails_both_event_and_annual_entry_points(self):
        event = implemented_dividends([RAW], DAY)[0]
        variants = [{**event, **change} for change in (
            {'cash_div_tax': '0.25'}, {'correction_source': 'https://example.test/claim'},
            {'correction_source_sha256': 'f' * 64}, {'correction_available_on': '20130410'},
            {'correction_fingerprint': 'fake'}, {'correction_originals': []},
            {'ts_code': '600099.SH'})]
        variants.append({k: v for k, v in event.items() if k != 'event_kind'})
        for change in ({'distribution_shares': '100'}, {'distribution_shares': '787225548.5'},
                       {'end_date': '20121231'}, {'cash_div_tax': '0.25'},
                       {'ordinary_cash_div_tax': '0'}, {'special_cash_div_tax': '0'}):
            changed = copy.deepcopy(event)
            changed['fiscal_components'][0].update(change)
            variants.append(changed)
        for changed in variants:
            with self.subTest(changed=changed):
                with self.assertRaises(AppError):
                    implemented_dividends([changed], DAY)
                with self.assertRaises(AppError):
                    annual_dividend_cash([changed], BALANCES)

    def test_invalid_source_share_amount_and_duplicate_registration_rejected(self):
        for change in ({'source': 'http://example.test/source'}, {'source_sha256': 'bad'},
                       {'pages': ''}, {'distribution_shares': '0'},
                       {'distribution_shares': '787225548.5'}, {'total_cash': '196806388.00'}):
            with self.subTest(change=change), patch.dict(DISCLOSURES, {'cash_basis': [{**PROOF, **change}]}):
                with self.assertRaises(AppError):
                    implemented_dividends([RAW], DAY)
        with patch.dict(DISCLOSURES, {'cash_basis': [PROOF, copy.deepcopy(PROOF)]}):
            with self.assertRaises(AppError):
                implemented_dividends([RAW], DAY)

    def test_direct_event_share_injection_is_never_trusted(self):
        injected = {**RAW, 'distribution_shares': '787225548'}
        for day in ('20130410', DAY):
            with self.subTest(day=day), self.assertRaises(AppError):
                implemented_dividends([injected], day)
        with self.assertRaises(AppError):
            annual_dividend_cash([injected], BALANCES)

    def test_real_ledger_keeps_900_share_holder_cash_at_225_after_late_proof(self):
        ledger = Ledger(':memory:', load_config())
        try:
            ledger.initialize('paper', '100000', '5000', '20000', '20120901')
            ledger.opening('paper', CODE, 900, '20120919')
            events = implemented_dividends([RAW], '20120921')
            ledger.dividends({CODE: events}, '20120921')
            self.assertEqual(ledger.cash('paper'), Decimal('100000'))
            ledger.dividends({CODE: events}, '20120927')
            old_entitlement = dict(ledger.db.execute('SELECT * FROM entitlements').fetchone())
            self.assertEqual(old_entitlement['quantity'], 900)
            self.assertEqual(Decimal(old_entitlement['dps']), Decimal('.25'))
            self.assertEqual(Decimal(old_entitlement['amount']), Decimal('225'))
            self.assertEqual(ledger.cash('paper'), Decimal('100225'))
            corrected = implemented_dividends([RAW], DAY)
            ledger.dividends({CODE: corrected}, DAY)
            ledger.dividends({CODE: corrected}, '20130504')
            self.assertEqual(dict(ledger.db.execute('SELECT * FROM entitlements').fetchone()), old_entitlement)
            self.assertEqual(ledger.cash('paper'), Decimal('100225'))
            self.assertEqual(ledger.positions('paper'), {CODE: 900})
            self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM entitlements').fetchone()[0], 1)
            account = ledger.summary('paper')
            self.assertEqual(len(account['cash_events']), 1)
            self.assertEqual(account['cash_events'][0]['amount'], '225.00')
            self.assertEqual(account['stock_events'], [])
        finally:
            ledger.close()


class AnnualCompositionGroupTests(unittest.TestCase):
    def setUp(self):
        # A structural two-row fixture; actual 600123 source facts have separate tests.
        self.proof = copy.deepcopy(PROOF)
        annual = copy.deepcopy(self.proof['annual_composition']['expected'])
        proposal = {**annual, 'ann_date': '20130411', 'div_proc': '预案', 'cash_div_tax': 0.0}
        self.originals = [proposal, annual]
        self.proof['annual_composition']['expected'] = copy.deepcopy(self.originals)
        self.rows = [copy.deepcopy(RAW), *copy.deepcopy(self.originals)]

    def decision(self, rows=None, day='20130504', proof=None):
        with patch.dict(DISCLOSURES, {'cash_basis': [self.proof if proof is None else proof]}):
            return annual_cash_decision(self.rows if rows is None else rows, CODE, '20121231', day, BALANCES)

    def test_exact_group_uses_its_independent_source_day_and_preserves_originals(self):
        saved = copy.deepcopy(self.rows)
        self.assertEqual(self.decision(day='20130503')['stage'], 'unresolved')
        result = self.decision()
        self.assertEqual(result['stage'], 'implemented')
        self.assertEqual(Decimal(result['total_cash']), Decimal('196806387.00'))
        self.assertEqual(Decimal(result['ordinary_cash']), Decimal('196806387.00'))
        self.assertNotIn('annual_original', result)
        expected = sorted(self.originals, key=lambda r: json.dumps(r, sort_keys=True))
        self.assertEqual(result['annual_originals'], expected)
        self.assertEqual(self.decision(list(reversed(self.rows))), result)
        reordered = copy.deepcopy(self.proof)
        reordered['annual_composition']['expected'].reverse()
        self.assertEqual(self.decision(proof=reordered), result)
        self.assertEqual(self.rows, saved)
        self.assertIsNone(result['annual_originals'][1]['cash_div_tax'])

    def test_missing_duplicate_modified_and_type_changed_originals_are_rejected(self):
        variants = [self.rows[:-1], self.rows + [copy.deepcopy(self.rows[-1])],
                    [self.rows[0], {**self.rows[1], 'cash_div_tax': 0}, self.rows[2]],
                    [self.rows[0], self.rows[1], {**self.rows[2], 'cash_div_tax': 0.0}],
                    [self.rows[0], self.rows[1], {**self.rows[2], 'extra_field': 'unverified'}],
                    [self.rows[0], self.rows[1], {k: v for k, v in self.rows[2].items() if k != 'stk_div'}]]
        for rows in variants:
            with self.subTest(rows=rows), self.assertRaises(AppError):
                self.decision(rows)

    def test_group_registration_requires_same_company_year_and_no_extra_positive_cash(self):
        for change in ({'ts_code': '600099.SH'}, {'end_date': '20111231'},
                       {'ann_date': '20130505'}, {'cash_div_tax': .1}):
            proof = copy.deepcopy(self.proof)
            proof['annual_composition']['expected'][0].update(change)
            rows = [copy.deepcopy(RAW), *copy.deepcopy(proof['annual_composition']['expected'])]
            with self.subTest(change=change), self.assertRaises(AppError):
                self.decision(rows, day='20130506', proof=proof)
        for originals in ([], self.originals + [copy.deepcopy(self.originals[0])],
                          [self.originals[0], None]):
            proof = copy.deepcopy(self.proof)
            proof['annual_composition']['expected'] = originals
            with self.subTest(originals=originals), self.assertRaises(AppError):
                self.decision(proof=proof)

    def test_future_revision_is_isolated_until_its_actual_publication(self):
        future_proposal = {**self.rows[-1], 'ann_date': '20130510', 'cash_div_tax': .1}
        future_implementation = {**self.rows[-1], 'ann_date': '20130503', 'imp_ann_date': '20130510',
                                 'div_proc': '实施', 'record_date': '20130513', 'ex_date': '20130514',
                                 'pay_date': '20130514', 'cash_div_tax': .1}
        for future in (future_proposal, future_implementation):
            with self.subTest(future=future):
                rows = self.rows + [future]
                self.assertEqual(self.decision(rows, '20130509'), self.decision(day='20130509'))
                with self.assertRaises(AppError):
                    self.decision(rows, '20130510')

    def test_legacy_single_row_keeps_annual_implementation_handoff(self):
        extra = {**RAW, 'end_date': '20121231', 'ann_date': '20130505', 'imp_ann_date': '20130506',
                 'record_date': '20130508', 'ex_date': '20130509', 'pay_date': '20130509', 'cash_div_tax': .1}
        rows = [RAW, PROOF['annual_composition']['expected'], extra]
        self.assertIsNone(self.decision(rows, '20130506', proof=PROOF))


if __name__ == '__main__':
    unittest.main()
