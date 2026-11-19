import tempfile
import unittest
from pathlib import Path
from contextlib import closing

from workflow_tasks import WorkflowTaskStore, WorkflowWorker
from workflow_runtime import build_instruction, dispatch_task, observe_events


class FakeClock:
    def __init__(self, value=1_800_000_000):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class WorkflowTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.store = WorkflowTaskStore(Path(self.temp.name) / 'tasks.sqlite', clock=self.clock,
                                       lease_seconds=60)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, request_id, target='thread-a', run_at=None, instruction='check task'):
        return self.store.create('scheduled', target, 'codex', instruction,
                                 self.clock() if run_at is None else run_at,
                                 request_id=request_id)

    def test_create_is_idempotent_and_rejects_request_id_reuse(self):
        first = self.create('mobile-submit-1')
        retry = self.create('mobile-submit-1')
        self.assertEqual(first['id'], retry['id'])
        self.assertEqual(first['cid'], retry['cid'])
        with self.assertRaisesRegex(ValueError, 'workflow_request_conflict'):
            self.create('mobile-submit-1', instruction='different payload')
        events = self.store.events(first['id'])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event_type'], 'command.accepted')
        with closing(self.store.connect()) as db:
            receipt = db.execute('SELECT task_id FROM workflow_commands WHERE request_id=?',
                                 ('mobile-submit-1',)).fetchone()
        self.assertEqual(receipt['task_id'], first['id'])

    def test_task_intent_command_receipt_and_event_rollback_together(self):
        with closing(self.store.connect()) as db, db:
            db.execute('''CREATE TRIGGER reject_workflow_event BEFORE INSERT ON workflow_events
                          BEGIN SELECT RAISE(ABORT, 'simulated event write failure'); END''')
        with self.assertRaisesRegex(Exception, 'simulated event write failure'):
            self.create('atomic-create')
        with closing(self.store.connect()) as db:
            task_count = db.execute('SELECT COUNT(*) FROM workflow_tasks').fetchone()[0]
            receipt_count = db.execute('SELECT COUNT(*) FROM workflow_commands').fetchone()[0]
            event_count = db.execute('SELECT COUNT(*) FROM workflow_events').fetchone()[0]
        self.assertEqual((task_count, receipt_count, event_count), (0, 0, 0))

    def test_state_update_and_transition_event_rollback_together(self):
        task = self.create('atomic-transition')
        with closing(self.store.connect()) as db, db:
            db.execute('''CREATE TRIGGER reject_workflow_event BEFORE INSERT ON workflow_events
                          BEGIN SELECT RAISE(ABORT, 'simulated event write failure'); END''')
        with self.assertRaisesRegex(Exception, 'simulated event write failure'):
            self.store.claim_due()
        self.assertEqual(self.store.get(task['id'])['state'], 'scheduled')
        self.assertEqual(len(self.store.events(task['id'])), 1)

    def test_due_tasks_for_one_target_are_claimed_serially(self):
        self.create('same-thread-1')
        self.create('same-thread-2')
        claimed = self.store.claim_due(limit=8)
        self.assertEqual(len(claimed), 1)
        with closing(self.store.connect()) as db:
            state = db.execute('SELECT state FROM workflow_tasks WHERE id=?', (claimed[0]['id'],)).fetchone()[0]
        self.assertEqual(state, 'dispatching')

    def test_expired_lease_reclaims_same_task_and_cid(self):
        task = self.create('reclaim-1')
        first = self.store.claim_due()[0]
        self.clock.advance(61)
        second = self.store.claim_due()[0]
        self.assertEqual(task['id'], second['id'])
        self.assertEqual(first['cid'], second['cid'])
        self.assertEqual(second['attempts'], 2)

    def test_old_schedule_requires_review_without_blocking_new_schedule(self):
        old = self.create('old-1', target='thread-old')
        self.clock.advance(1801)
        fresh = self.create('fresh-1', target='thread-old', run_at=self.clock())
        claimed = self.store.claim_due()
        self.assertEqual([row['id'] for row in claimed], [fresh['id']])
        self.assertEqual(self.store.get(old['id'])['state'], 'needs_review')

    def test_uncertain_dispatch_is_never_retried_automatically(self):
        task = self.create('uncertain-1')
        self.store.claim_due()
        self.store.mark_uncertain(task['id'])
        self.clock.advance(601)
        reviewed = self.store.review_uncertain(task['id'])
        self.assertEqual(reviewed['state'], 'needs_review')
        self.assertTrue(reviewed['outcomeUnknown'])
        self.assertEqual(self.store.claim_due(), [])
        with self.assertRaisesRegex(ValueError, 'workflow_outcome_unknown_requires_reconciliation'):
            self.store.resume(task['id'])
        with self.assertRaisesRegex(ValueError, 'workflow_already_dispatched'):
            self.store.cancel(task['id'])
        self.assertEqual(self.store.retry_reconciliation(task['id'])['state'], 'reconciling')

    def test_unknown_outcome_blocks_later_task_until_user_resumes_or_resolves(self):
        uncertain = self.create('uncertain-blocker')
        self.store.claim_due()
        self.store.mark_uncertain(uncertain['id'])
        self.clock.advance(601)
        self.store.review_uncertain(uncertain['id'])
        self.create('later-task')
        self.assertEqual(self.store.claim_due(), [])

    def test_cancellation_prevents_claim(self):
        task = self.create('cancel-1')
        self.store.cancel(task['id'])
        self.assertEqual(self.store.claim_due(), [])
        self.assertEqual(self.store.get(task['id'])['state'], 'cancelled')

    def test_dispatch_exception_becomes_uncertain_not_accepted(self):
        task = self.create('worker-unknown')
        worker = WorkflowWorker(self.store, lambda _: (_ for _ in ()).throw(TimeoutError()),
                                lambda _: None, logger=lambda _: None)
        worker.tick()
        result = self.store.get(task['id'])
        self.assertEqual(result['state'], 'reconciling')
        self.assertTrue(result['outcomeUnknown'])

    def test_gateway_5xx_never_automatically_retries_submission(self):
        task = self.create('gateway-5xx')
        self.store.claim_due()
        result = self.store.dispatch_result(task['id'], {'error': {'message': 'upstream timeout'}}, 503)
        self.assertEqual(result['state'], 'reconciling')
        self.clock.advance(601)
        result = self.store.review_uncertain(task['id'])
        self.assertEqual(result['state'], 'needs_review')
        self.assertEqual(self.store.claim_due(), [])

    def test_reconciliation_poll_does_not_reset_unknown_timeout(self):
        task = self.create('poll-unknown')
        self.store.claim_due()
        self.store.mark_uncertain(task['id'])
        accepted = {'mode': 'accepted'}
        self.clock.advance(500)
        self.store.reconcile(task['id'], accepted)
        self.clock.advance(101)
        result = self.store.reconcile(task['id'], accepted)
        self.assertEqual(result['state'], 'reconciling')
        self.assertEqual(self.store.review_uncertain(task['id'])['state'], 'needs_review')

    def test_repeated_local_preflight_deferrals_do_not_strand_task(self):
        task = self.create('preflight-wait')
        for _ in range(12):
            claimed = self.store.claim_due()
            self.assertEqual(len(claimed), 1)
            self.store.dispatch_result(task['id'], {'deferred': True, 'retryAt': self.clock()+30}, 202)
            self.clock.advance(31)
        self.assertEqual(len(self.store.claim_due()), 1)


class RuntimeTests(unittest.TestCase):
    def test_handoff_contains_only_explicit_instruction_and_source_identity(self):
        text = build_instruction({'kind': 'handoff', 'source_thread_id': 'source-1',
                                  'source_engine': 'claude', 'instruction': 'run focused tests'})
        self.assertIn('claude/source-1', text)
        self.assertIn('run focused tests', text)
        self.assertNotIn('history dump', text)

    def test_dispatch_retry_resolves_existing_cid_without_resubmitting(self):
        class Ledger:
            def __init__(self):
                self.receipt = None
                self.calls = 0

            def lookup(self, _tid, cid):
                return {'found': bool(self.receipt), 'cid': cid}

            def run(self, _tid, body, submit, _reconcile):
                self.calls += 1
                self.receipt = submit()
                self.body = body
                return self.receipt, 200

            def resolve(self, _tid, _cid, _reconcile):
                return self.receipt, 200

        ledger = Ledger()
        task = {'target_thread_id': 'target', 'target_engine': 'codex', 'cid': 'stable-cid',
                'kind': 'scheduled', 'instruction': 'run once'}
        submit = lambda _tid, body: {'mode': 'queued', 'body': body}
        dispatch_task(task, ledger, lambda _: None, submit, lambda *_: None)
        dispatch_task(task, ledger, lambda _: None, submit, lambda *_: None)
        self.assertEqual(ledger.calls, 1)
        self.assertEqual(ledger.body['clientUserMessageId'], 'stable-cid')

    def test_terminal_event_must_match_exact_turn(self):
        task = {'target_thread_id': 'target', 'turn_id': 'turn-2', 'event_cursor': 8}
        events = [
            {'seq': 9, 'method': 'turn/completed', 'params': {'threadId': 'target', 'turnId': 'turn-1'}},
            {'seq': 10, 'method': 'turn/completed', 'params': {'threadId': 'other', 'turnId': 'turn-2'}},
            {'seq': 11, 'method': 'turn/completed', 'params': {'threadId': 'target', 'turnId': 'turn-2'}},
        ]
        cursor, terminal = observe_events(task, events)
        self.assertEqual(cursor, 11)
        self.assertEqual(terminal, ('completed', ''))


if __name__ == '__main__':
    unittest.main()
