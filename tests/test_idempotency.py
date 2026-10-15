"""Focused checks for shared idempotency and reservation behavior."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier
import time
import unittest

from jsonschema import Draft202012Validator
import yaml

from app import app
import idempotency
import services
import storage


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        names = ('EVENTS', 'RESERVATIONS', 'IDEMPOTENCY', 'WAITLIST', 'NEXT')
        self.original = {name: deepcopy(getattr(storage, name)) for name in names}
        storage.IDEMPOTENCY.clear()
        storage.RESERVATIONS.clear()
        storage.WAITLIST.clear()
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

    def test_waitlist_join_replays_original_entry_and_position(self):
        first = services.join_waitlist('ev_102', 'join-first', b'{"userId":"alice"}')
        second = services.join_waitlist('ev_102', 'join-second', b'{"userId":"bob"}')
        self.assertEqual(first[1:], (201, False))
        self.assertEqual(second[0]['position'], 2)
        storage.WAITLIST['ev_102'][0]['createdAt'] = 'changed'
        self.assertEqual(services.join_waitlist('ev_102', 'join-first', b'{"userId":"alice"}'),
                         (first[0], 201, True))
        self.assertEqual(services.join_waitlist('ev_102', 'join-first', b'{ "userId":"alice" }'),
                         ({'error': 'idempotency_key_reuse'}, 422, False))
        self.assertEqual(len(storage.WAITLIST['ev_102']), 2)
        spec = yaml.safe_load((Path(__file__).resolve().parents[1] /
                               'contracts/encore-openapi.yaml').read_text())
        Draft202012Validator({'$ref': '#/components/schemas/WaitlistEntry',
                              'components': spec['components']}).validate(first[0])

    def test_waitlist_errors_are_stored(self):
        cases = [('ev_101', b'{"userId":"alice"}', 409, 'event_not_full'),
                 ('ev_unknown', b'{"userId":"alice"}', 404, 'event_not_found')]
        for i, (event, body, status, error) in enumerate(cases):
            key = f'error-key-{i}'
            first = services.join_waitlist(event, key, body)
            storage.EVENTS[event] = {'id': event, 'seatsLeft': 0}
            self.assertEqual(first, ({'error': error}, status, False))
            self.assertEqual(services.join_waitlist(event, key, body), (first[0], status, True))
        services.join_waitlist('ev_102', 'original-user', b'{"userId":"alice"}')
        duplicate = services.join_waitlist('ev_102', 'duplicate-user', b'{"userId":"alice"}')
        storage.WAITLIST['ev_102'].clear()
        self.assertEqual(duplicate, ({'error': 'already_on_waitlist'}, 409, False))
        self.assertEqual(services.join_waitlist('ev_102', 'duplicate-user', b'{"userId":"alice"}'),
                         (duplicate[0], 409, True))

    def test_validation_runs_inside_helper_and_replays(self):
        bad_bodies = [b'bad', b'\xff', b'[]', b'{}', b'{"userId":1}',
                      b'{"userId":""}', b'{"userId":"a","extra":true}']
        for i, body in enumerate(bad_bodies):
            key = f'invalid-key-{i}'
            self.assertEqual(services.join_waitlist('ev_102', key, body),
                             ({'error': 'invalid_request'}, 422, False))
            self.assertEqual(services.join_waitlist('ev_102', key, body),
                             ({'error': 'invalid_request'}, 422, True))
        self.assertEqual(services.join_waitlist('ev_102', None, b'bad'),
                         ({'error': 'idempotency_key_required'}, 422, False))
        self.assertEqual(storage.WAITLIST, {})

    def test_global_key_reuse_across_requests_is_rejected(self):
        other_event_seats = storage.EVENTS['ev_103']['seatsLeft']
        first = services.create_reservation('ev_101', 'global-key', b'{}')
        self.assertEqual(services.join_waitlist('ev_102', 'global-key', b'{}'),
                         ({'error': 'idempotency_key_reuse'}, 422, False))
        self.assertEqual(services.create_reservation('ev_103', 'global-key', b'{}'),
                         ({'error': 'idempotency_key_reuse'}, 422, False))
        self.assertEqual(services.create_reservation('ev_101', 'global-key', b'{}'),
                         (first[0], 201, True))
        self.assertEqual(storage.EVENTS['ev_103']['seatsLeft'], other_event_seats)
        self.assertEqual(len(storage.RESERVATIONS), 1)
        self.assertEqual(storage.WAITLIST, {})

    def test_routes_keep_replay_header_and_delay_only_first_reply(self):
        from unittest.mock import patch
        headers = {'Idempotency-Key': 'route-reservation', 'X-Demo-Lose-Reply': 'true'}
        with patch('routes.time.sleep') as sleep:
            first = self.client.post('/events/ev_101/reservations', headers=headers)
            replay = self.client.post('/events/ev_101/reservations', headers=headers)
        sleep.assert_called_once_with(3)
        self.assertEqual(first.status_code, 201)
        self.assertNotIn('Idempotency-Replayed', first.headers)
        self.assertEqual(replay.headers['Idempotency-Replayed'], 'true')
        self.assertEqual(first.json, replay.json)
        headers = {'Idempotency-Key': 'route-waitlist'}
        first = self.client.post('/events/ev_102/waitlist', headers=headers,
                                 json={'userId': 'alice'})
        replay = self.client.post('/events/ev_102/waitlist', headers=headers,
                                  json={'userId': 'alice'})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.json, first.json)
        self.assertEqual(replay.headers['Idempotency-Replayed'], 'true')


if __name__ == '__main__':
    unittest.main()
