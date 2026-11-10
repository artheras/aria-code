import unittest

from aria_code.runtime.task_outcome import task_outcome


class TaskOutcomeTests(unittest.TestCase):
    def test_done_is_not_verification(self):
        self.assertIsNone(task_outcome({'status': 'done', 'success': True})['verified'])

    def test_requires_consistent_host_report(self):
        base = {'status': 'done', 'verified': True, 'verification': {'status': 'verified', 'verified': True, 'checks': [{}]}}
        self.assertEqual(task_outcome(base, fresh=True)['verification_state'], 'verified')
        self.assertIsNone(task_outcome(base)['verified'])
        self.assertIsNone(task_outcome({**base, 'success': False}, fresh=True)['verified'])
        self.assertIsNone(task_outcome({**base, 'verification': {**base['verification'], 'checks': []}}, fresh=True)['verified'])
        for field, value in [('status', 'running'), ('verified', 'true')]:
            self.assertIsNone(task_outcome({**base, field: value}, fresh=True)['verified'])
        self.assertIsNone(task_outcome({**base, 'verification': {'status': 'stale', 'verified': True}}, fresh=True)['verified'])

    def test_failed_checks_and_failed_execution_are_separate(self):
        record = {'status': 'done', 'verified': False, 'verification': {'status': 'failed', 'verified': False, 'checks': [{}]}}
        self.assertIs(task_outcome(record, fresh=True)['verified'], False)
        record['status'] = 'failed'
        view = task_outcome(record, fresh=True)
        self.assertEqual(view['execution_state'], 'failed')
        self.assertIsNone(view['verified'])

    def test_raw_evidence_and_context_are_not_copied(self):
        record = {'status': 'done', 'evidence': ['secret'], 'context_sources': ['private'], 'result': 'VERIFIED', 'verification': {'checks': ['secret']}}
        view = task_outcome(record, fresh=True)
        self.assertEqual(view['checks_total'], 1)
        self.assertNotIn('secret', str(view))
        self.assertNotIn('private', str(view))

    def test_bad_report_shape_is_unverified(self):
        for value in [None, [], 'verified', {'checks': 'text'}]:
            self.assertEqual(task_outcome({'verification': value})['verification_state'], 'unverified')
