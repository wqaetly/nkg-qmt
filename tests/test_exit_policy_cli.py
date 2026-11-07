import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qmt.__main__ import main, parser
from qmt.config import atomic_json


class ExitPolicyCliTests(unittest.TestCase):
    def test_only_explicit_isolated_exit_experiment_changes_protocol(self):
        with tempfile.TemporaryDirectory() as temp, patch('qmt.__main__.render'), \
             patch('qmt.experiment.run_experiment') as run, \
             patch('sys.stdout',new_callable=io.StringIO), patch('sys.stderr',new_callable=io.StringIO):
            root=Path(temp);atomic_json(root/'screen.json',{'as_of':'20261106'})
            prefix=['--workspace',str(root),'backtest']
            flags=['--annual-pool','--all-candidates','--yield-rank','--approved-annual-cash',
                   '--three-year-cashflow','--equity-limits']
            for args in ([],flags+['--pe-context'],flags+['--earnings-basis','ttm']):
                self.assertEqual(main(prefix+args+['--known-failure-exit']),1)
                run.assert_not_called()
            for extra in ([],['--long-history','--position-freeze']):
                self.assertEqual(main(prefix+flags+extra),0)
                old=run.call_args.kwargs['experiment']
                self.assertNotIn('exit_policy',old)
                self.assertEqual(main(prefix+flags+extra+['--known-failure-exit']),0)
                new=run.call_args.kwargs['experiment']
                self.assertEqual(new,{**old,
                    'version':old['version'].replace('historical-dividend-v33','historical-dividend-v36',1)+'-known-failure-exit',
                    'exit_policy':'known_failure_exit'})
        self.assertFalse(hasattr(parser().parse_args(['daily']),'known_failure_exit'))
        self.assertEqual(parser().parse_args(['forecast-check']).version,
                         'historical-dividend-v33-annual-all-rank-approved-cash-three-year-fcf-equity-limits')


if __name__=='__main__':unittest.main()
