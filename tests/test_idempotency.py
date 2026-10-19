"""Focused checks for idempotency, reservation and waitlist services, and routes."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Barrier
import time
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
import yaml

from app import app
import idempotency
import services
import storage

CONTRACT = Path(__file__).resolve().parents[1] / 'contracts' / 'encore-openapi.yaml'
spec = yaml.safe_load(CONTRACT.read_text())


def schema_for(path, method, status):
    """Read a response schema and include components for reference resolution."""
    schema = (
        spec['paths'][path][method]['responses'][status]['content'][
            'application/json'
        ]['schema']
    )
    return {**schema, 'components': spec['components']}


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

    # Shared idempotency helper checks
    def test_missing_key_does_not_call_operation(self):
        def forbidden():
            self.fail('Operation ran without a key')

        result = idempotency.execute_idempotently(
            None, b'', 'test operation', forbidden
        )
        self.assertEqual(result, ({'error': 'idempotency_key_required'}, 422, False))
        self.assertEqual(storage.IDEMPOTENCY, {})

    def test_replay_snapshot_survives_domain_and_caller_mutation(self):
        domain = {'nested': {'position': 1}}

        first = idempotency.execute_idempotently(
            'snapshot-key', b'', 'test operation', lambda: (domain, 201)
        )

        # Changing a returned body must not change the stored snapshot.
        first[0]['nested']['position'] = 20

        replay = idempotency.execute_idempotently(
            'snapshot-key', b'', 'test operation', lambda: self.fail()
        )
        self.assertEqual(replay, ({'nested': {'position': 1}}, 201, True))

        # A replay must also be safe for the caller to modify.
        replay[0]['nested']['position'] = 30

        replay = idempotency.execute_idempotently(
            'snapshot-key', b'', 'test operation', lambda: self.fail()
        )
        self.assertEqual(replay, ({'nested': {'position': 1}}, 201, True))

    def test_outcome_expires_at_24_hour_boundary(self):
        calls = []
        retention_seconds = 24 * 60 * 60

        class FakeClock:
            def __init__(self):
                self.current = 0

            def __call__(self):
                return self.current

            def advance(self, seconds):
                self.current += seconds

        clock = FakeClock()

        def operation():
            calls.append(len(calls) + 1)

            # Retention starts after this operation finishes.
            clock.advance(10)
            return {'attempt': calls[-1]}, 201

        with patch('idempotency.monotonic', new=clock):
            first = idempotency.execute_idempotently(
                'expiring-key', b'{}', 'test operation', operation
            )

            # Check replay just before expiry and execution at the boundary.
            clock.advance(retention_seconds - 1)
            before_expiry = idempotency.execute_idempotently(
                'expiring-key', b'{}', 'test operation', operation
            )
            clock.advance(1)
            at_expiry = idempotency.execute_idempotently(
                'expiring-key', b'{}', 'test operation', operation
            )

            # The replacement outcome receives its own retention window.
            clock.advance(retention_seconds - 1)
            replacement_before_expiry = idempotency.execute_idempotently(
                'expiring-key', b'{}', 'test operation', operation
            )
            clock.advance(1)
            replacement_at_expiry = idempotency.execute_idempotently(
                'expiring-key', b'{}', 'test operation', operation
            )

        self.assertEqual(first, ({'attempt': 1}, 201, False))
        self.assertEqual(before_expiry, ({'attempt': 1}, 201, True))
        self.assertEqual(at_expiry, ({'attempt': 2}, 201, False))
        self.assertEqual(replacement_before_expiry, ({'attempt': 2}, 201, True))
        self.assertEqual(replacement_at_expiry, ({'attempt': 3}, 201, False))
        self.assertEqual(calls, [1, 2, 3])

    def test_concurrent_requests_execute_once(self):
        # Release all eight requests together to exercise concurrent replay.
        barrier = Barrier(8)
        calls = []

        def operation():
            calls.append(True)
            time.sleep(0.02)
            return {'id': 'one'}, 201

        def request(_):
            barrier.wait(timeout=5)
            return idempotency.execute_idempotently(
                'concurrent-key', b'{}', 'test operation', operation
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(request, range(8)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(not result[2] for result in results), 1)
        self.assertTrue(all(result[:2] == ({'id': 'one'}, 201) for result in results))

    def test_exception_propagates_and_releases_lock(self):
        def fail():
            raise RuntimeError('unexpected')

        with self.assertRaisesRegex(RuntimeError, 'unexpected'):
            idempotency.execute_idempotently(
                'exception-key', b'', 'test operation', fail
            )
        self.assertNotIn('exception-key', storage.IDEMPOTENCY)
        result = idempotency.execute_idempotently(
            'exception-key', b'', 'test operation', lambda: ({}, 201)
        )
        self.assertEqual(result, ({}, 201, False))

    # Reservation and shared-key service checks
    def test_exact_bytes_replay_and_changed_bytes_reject_without_mutation(self):
        seats = storage.EVENTS['ev_101']['seatsLeft']
        original_body = b'{}'

        first = services.create_reservation(
            'ev_101', 'reserve-key', original_body, 'application/json'
        )
        replay = services.create_reservation(
            'ev_101', 'reserve-key', original_body, 'application/json'
        )
        self.assertEqual(first[1:], (201, False))
        self.assertEqual(replay, (first[0], 201, True))

        changed_bodies = [
            b'{ }',
            b'{"unexpected":true}',
        ]
        for body in changed_bodies:
            with self.subTest(body=body):
                rejected = services.create_reservation(
                    'ev_101', 'reserve-key', body, 'application/json'
                )
                self.assertEqual(
                    rejected,
                    ({'error': 'idempotency_key_reuse'}, 422, False),
                )

        replay = services.create_reservation(
            'ev_101', 'reserve-key', original_body, 'application/json'
        )
        self.assertEqual(replay, (first[0], 201, True))
        self.assertEqual(storage.EVENTS['ev_101']['seatsLeft'], seats - 1)
        self.assertEqual(len(storage.RESERVATIONS), 1)

    def test_reservation_errors_replay_after_state_changes(self):
        for event_id, code, status in [
            ('ev_102', 'sold_out', 409),
            ('ev_999', 'not_found', 404),
        ]:
            with self.subTest(event_id=event_id):
                first = services.create_reservation(event_id, event_id, b'', None)

                # Make a fresh request succeed so the retry must use its stored error.
                if event_id == 'ev_999':
                    storage.EVENTS[event_id] = {'id': event_id, 'seatsLeft': 5}
                else:
                    storage.EVENTS[event_id]['seatsLeft'] = 5

                self.assertEqual(first, ({'error': code}, status, False))

                replay = services.create_reservation(event_id, event_id, b'', None)
                self.assertEqual(replay, (first[0], status, True))
        self.assertEqual(storage.RESERVATIONS, {})

    def test_global_key_reuse_across_requests_is_rejected(self):
        other_event_seats = storage.EVENTS['ev_103']['seatsLeft']
        first = services.create_reservation(
            'ev_101', 'global-key', b'{}', 'application/json'
        )

        rejected = services.join_waitlist(
            'ev_102', 'global-key', b'{}', 'application/json'
        )
        self.assertEqual(rejected, ({'error': 'idempotency_key_reuse'}, 422, False))

        rejected = services.create_reservation(
            'ev_103', 'global-key', b'{}', 'application/json'
        )
        self.assertEqual(rejected, ({'error': 'idempotency_key_reuse'}, 422, False))

        replay = services.create_reservation(
            'ev_101', 'global-key', b'{}', 'application/json'
        )
        self.assertEqual(replay, (first[0], 201, True))
        self.assertEqual(storage.EVENTS['ev_103']['seatsLeft'], other_event_seats)
        self.assertEqual(len(storage.RESERVATIONS), 1)
        self.assertEqual(storage.WAITLIST, {})

    def test_waitlist_join_replays_original_entry_and_position(self):
        first = services.join_waitlist(
            'ev_102', 'join-first', b'{"userId":"alice"}', 'application/json'
        )
        second = services.join_waitlist(
            'ev_102', 'join-second', b'{"userId":"bob"}', 'application/json'
        )
        self.assertEqual(first[1:], (201, False))
        self.assertEqual(second[0]['position'], 2)

        # Changing the stored entry must not change the original POST outcome.
        storage.WAITLIST['ev_102'][0]['createdAt'] = 'changed'
        replay = services.join_waitlist(
            'ev_102', 'join-first', b'{"userId":"alice"}', 'application/json'
        )
        self.assertEqual(replay, (first[0], 201, True))

        rejected = services.join_waitlist(
            'ev_102', 'join-first', b'{ "userId":"alice" }', 'application/json'
        )
        self.assertEqual(rejected, ({'error': 'idempotency_key_reuse'}, 422, False))
        self.assertEqual(len(storage.WAITLIST['ev_102']), 2)
        Draft202012Validator(
            schema_for('/events/{eventId}/waitlist', 'post', '201')
        ).validate(first[0])

    def test_waitlist_errors_are_stored(self):
        cases = [
            ('ev_101', b'{"userId":"alice"}', 409, 'event_not_full'),
            ('ev_999', b'{"userId":"alice"}', 404, 'event_not_found'),
        ]
        for i, (event, body, status, error) in enumerate(cases):
            key = f'error-key-{i}'
            first = services.join_waitlist(event, key, body, 'application/json')

            # Change the event so a fresh join would succeed.
            storage.EVENTS[event] = {'id': event, 'seatsLeft': 0}

            self.assertEqual(first, ({'error': error}, status, False))

            replay = services.join_waitlist(event, key, body, 'application/json')
            self.assertEqual(replay, (first[0], status, True))

        services.join_waitlist(
            'ev_102', 'original-user', b'{"userId":"alice"}', 'application/json'
        )
        duplicate = services.join_waitlist(
            'ev_102', 'duplicate-user', b'{"userId":"alice"}', 'application/json'
        )

        # Remove the user so the retry proves the earlier error was stored.
        storage.WAITLIST['ev_102'].clear()
        self.assertEqual(duplicate, ({'error': 'already_on_waitlist'}, 409, False))
        replay = services.join_waitlist(
            'ev_102', 'duplicate-user', b'{"userId":"alice"}', 'application/json'
        )
        self.assertEqual(replay, (duplicate[0], 409, True))

    def test_validation_runs_inside_helper_and_replays(self):
        bad_bodies = [
            b'bad',
            b'\xff',
            b'[]',
            b'{}',
            b'{"userId":1}',
            b'{"userId":""}',
            b'{"userId":"a","extra":true}',
        ]
        for i, body in enumerate(bad_bodies):
            key = f'invalid-key-{i}'
            result = services.join_waitlist(
                'ev_102', key, body, 'application/json'
            )
            self.assertEqual(result, ({'error': 'invalid_request'}, 422, False))

            replay = services.join_waitlist(
                'ev_102', key, body, 'application/json'
            )
            self.assertEqual(replay, ({'error': 'invalid_request'}, 422, True))

        result = services.join_waitlist(
            'ev_102', None, b'bad', 'application/json'
        )
        self.assertEqual(result, ({'error': 'idempotency_key_required'}, 422, False))
        self.assertEqual(storage.WAITLIST, {})

    # Route and contract checks
    def test_routes_keep_replay_header_and_delay_only_first_reply(self):
        # Reservation replay and first-response delay.
        headers = {'Idempotency-Key': 'route-reservation', 'X-Demo-Lose-Reply': 'true'}
        with patch('routes.time.sleep') as sleep:
            first = self.client.post('/events/ev_101/reservations', headers=headers)
            replay = self.client.post('/events/ev_101/reservations', headers=headers)

        sleep.assert_called_once_with(3)
        self.assertEqual(first.status_code, 201)
        self.assertNotIn('Idempotency-Replayed', first.headers)
        self.assertEqual(replay.headers['Idempotency-Replayed'], 'true')
        self.assertEqual(first.json, replay.json)

        # Waitlist replay header and response body.
        headers = {'Idempotency-Key': 'route-waitlist'}
        first = self.client.post(
            '/events/ev_102/waitlist',
            headers=headers,
            json={'userId': 'alice'},
        )
        replay = self.client.post(
            '/events/ev_102/waitlist',
            headers=headers,
            json={'userId': 'alice'},
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.json, first.json)
        self.assertEqual(replay.headers['Idempotency-Replayed'], 'true')

    def test_routes_validate_parameters_content_types_and_optional_bodies(self):
        self.assertEqual(self.client.get('/events?bookable=maybe').status_code, 422)
        self.assertEqual(self.client.get('/events/not-an-event').status_code, 422)
        self.assertEqual(self.client.get('/reservations/not-a-reservation').status_code, 422)

        reservation = self.client.post(
            '/events/ev_101/reservations',
            headers={'Idempotency-Key': 'optional-body'},
        )
        self.assertEqual(reservation.status_code, 201)

        valid_waitlist = self.client.post(
            '/events/ev_102/waitlist',
            headers={'Idempotency-Key': 'charset-body',
                     'Content-Type': 'application/json; charset=utf-8'},
            data=b'{"userId":"charset-user"}',
        )
        self.assertEqual(valid_waitlist.status_code, 201)

        # Each invalid request must return the documented validation error.
        for key, headers, body in [
            ('missing-body', {'Idempotency-Key': 'missing-body'}, None),
            (
                'missing-type',
                {'Idempotency-Key': 'missing-type'},
                b'{"userId":"missing-type"}',
            ),
            (
                'wrong-type',
                {'Idempotency-Key': 'wrong-type', 'Content-Type': 'text/plain'},
                b'{"userId":"wrong-type"}',
            ),
            (
                'bad-json',
                {
                    'Idempotency-Key': 'bad-json',
                    'Content-Type': 'application/json',
                },
                b'{',
            ),
        ]:
            with self.subTest(key=key):
                response = self.client.post(
                    '/events/ev_102/waitlist',
                    headers=headers,
                    data=body,
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json, {'error': 'invalid_request'})

    def test_validation_errors_match_error_schema_and_replay(self):
        response = self.client.post(
            '/events/ev_102/waitlist',
            headers={'Idempotency-Key': 'invalid-replay',
                     'Content-Type': 'application/json'},
            data=b'{"userId":""}',
        )
        replay = self.client.post(
            '/events/ev_102/waitlist',
            headers={'Idempotency-Key': 'invalid-replay',
                     'Content-Type': 'application/json'},
            data=b'{"userId":""}',
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(replay.status_code, 422)
        self.assertEqual(replay.headers['Idempotency-Replayed'], 'true')
        Draft202012Validator(
            schema_for('/events/{eventId}/waitlist', 'post', '422')
        ).validate(response.json)


if __name__ == '__main__':
    unittest.main()
