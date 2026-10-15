"""Focused checks for shared idempotency and reservation behavior."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
import time
import unittest

from app import app
import idempotency
import services
import storage


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        names = ('EVENTS', 'RESERVATIONS', 'IDEMPOTENCY', 'NEXT')
        self.original = {name: deepcopy(getattr(storage, name)) for name in names}
        storage.IDEMPOTENCY.clear()
        storage.RESERVATIONS.clear()
        self.client = app.test_client()

    def tearDown(self):
        for name, value in self.original.items():
            target = getattr(storage, name)
            target.clear()
            target.update(value)

    def test_missing_key_does_not_call_operation(self):
        def forbidden():
            self.fail('Operation ran without a key')
        self.assertEqual(idempotency.execute_idempotently(None, b'', 'test operation', forbidden),
                         ({'error': 'idempotency_key_required'}, 422, False))
        self.assertEqual(storage.IDEMPOTENCY, {})

    def test_exact_bytes_replay_and_changed_bytes_reject_without_mutation(self):
        seats = storage.EVENTS['ev_101']['seatsLeft']
        original_body = b'{"items":[1,2],"userId":"u1","count":1}'
        first = services.create_reservation('ev_101', 'reserve-key', original_body)
        replay = services.create_reservation('ev_101', 'reserve-key', original_body)
        self.assertEqual(first[1:], (201, False))
        self.assertEqual(replay, (first[0], 201, True))

        changed_bodies = [
            b'{ "items":[1,2],"userId":"u1","count":1}',
            b'{"count":1,"items":[1,2],"userId":"u1"}',
            b'{"items":[2,1],"userId":"u1","count":1}',
            b'{"items":[1,2],"userId":"u2","count":1}',
            b'{"items":[1,2],"userId":"u1","count":1.0}',
        ]
        for body in changed_bodies:
            with self.subTest(body=body):
                self.assertEqual(
                    services.create_reservation('ev_101', 'reserve-key', body),
                    ({'error': 'idempotency_key_reuse'}, 422, False),
                )

        self.assertEqual(services.create_reservation('ev_101', 'reserve-key', original_body), replay)
        self.assertEqual(storage.EVENTS['ev_101']['seatsLeft'], seats - 1)
        self.assertEqual(len(storage.RESERVATIONS), 1)

    def test_reservation_errors_replay_after_state_changes(self):
        for event_id, code, status in [('ev_102', 'sold_out', 409),
                                       ('ev_unknown', 'not_found', 404)]:
            with self.subTest(event_id=event_id):
                first = services.create_reservation(event_id, event_id, b'')
                storage.EVENTS[event_id] = {'id': event_id, 'seatsLeft': 5}
                self.assertEqual(first, ({'error': code}, status, False))
                self.assertEqual(services.create_reservation(event_id, event_id, b''),
                                 (first[0], status, True))
        self.assertEqual(storage.RESERVATIONS, {})

    def test_replay_snapshot_survives_domain_and_caller_mutation(self):
        domain = {'nested': {'position': 1}}
        first = idempotency.execute_idempotently('snapshot-key', b'', 'test operation',
                                     lambda: (domain, 201))
        first[0]['nested']['position'] = 20
        replay = idempotency.execute_idempotently('snapshot-key', b'', 'test operation',
                                      lambda: self.fail())
        self.assertEqual(replay, ({'nested': {'position': 1}}, 201, True))
        replay[0]['nested']['position'] = 30
        self.assertEqual(idempotency.execute_idempotently('snapshot-key', b'', 'test operation',
                                              lambda: self.fail()),
                         ({'nested': {'position': 1}}, 201, True))

    def test_concurrent_requests_execute_once(self):
        barrier = Barrier(8)
        calls = []

        def operation():
            calls.append(True)
            time.sleep(0.02)
            return {'id': 'one'}, 201

        def request(_):
            barrier.wait(timeout=5)
            return idempotency.execute_idempotently('concurrent-key', b'{}', 'test operation',
                                        operation)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(request, range(8)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(not result[2] for result in results), 1)
        self.assertTrue(all(result[:2] == ({'id': 'one'}, 201) for result in results))

    def test_exception_propagates_and_releases_lock(self):
        def fail():
            raise RuntimeError('unexpected')
        with self.assertRaisesRegex(RuntimeError, 'unexpected'):
            idempotency.execute_idempotently('exception-key', b'', 'test operation', fail)
        self.assertNotIn('exception-key', storage.IDEMPOTENCY)
        self.assertEqual(idempotency.execute_idempotently('exception-key', b'', 'test operation',
                                              lambda: ({}, 201)),
                         ({}, 201, False))


if __name__ == '__main__':
    unittest.main()
