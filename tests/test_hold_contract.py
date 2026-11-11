"""Check the published contract: Hold operations, v1.1 compatibility and 503 coverage.

Most tests read the YAML only. The 503 test also sends every operation through
the Flask app on an in-memory adapter whose units of work always abort, so it
shows which handlers retry and give 503; it does not exercise PostgreSQL.
Run: python3 -m unittest discover -s tests -p 'test_hold*.py' -v
"""

from contextlib import contextmanager
from pathlib import Path
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
import yaml

from app import app
from booking_repository import TransactionAborted
import storage


ROOT = Path(__file__).resolve().parents[1]
SPEC = yaml.safe_load((ROOT / 'contracts/encore-openapi.yaml').read_text())
CREATE = '/events/{eventId}/holds'


def resolve(reference):
    """Resolve a local OpenAPI JSON pointer in the published contract."""
    value = SPEC
    for part in reference.removeprefix('#/').split('/'):
        value = value[part.replace('~1', '/').replace('~0', '~')]
    return value


def validator(schema):
    """Validate schemas using the same component context as conformance."""
    return Draft202012Validator(
        {**schema, 'components': SPEC['components']},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


class HoldContractTests(unittest.TestCase):
    def test_component_references_examples_and_legacy_contract_are_valid(self):
        """Add Hold routes; v1.1 operations change only by listing a 503 response."""
        baseline = yaml.safe_load((ROOT / 'tests/fixtures/encore-v1.1.0.yaml').read_text())
        for path, operations in baseline['paths'].items():
            for method, operation in operations.items():
                current = SPEC['paths'][path][method]
                responses = {k: v for k, v in current['responses'].items() if k != '503'}
                self.assertEqual({**current, 'responses': responses}, operation, (path, method))
        for name, schema in baseline['components']['schemas'].items():
            self.assertEqual(SPEC['components']['schemas'][name], schema, name)
        for schema in SPEC['components']['schemas'].values():
            Draft202012Validator.check_schema(schema)

        def visit(value):
            """Resolve every reference and validate each documented JSON response example."""
            if isinstance(value, dict):
                if '$ref' in value:
                    self.assertTrue(value['$ref'].startswith('#/'))
                    resolve(value['$ref'])
                if 'schema' in value:
                    examples = [e['value'] for e in value.get('examples', {}).values()]
                    if 'example' in value:
                        examples.append(value['example'])
                    for example in examples:
                        validator(value['schema']).validate(example)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(SPEC)

    def test_creation_accepts_only_an_optional_empty_object_and_returns_receipt(self):
        """A Hold allocates one seat with no client-controlled booking options."""
        operation = SPEC['paths'][CREATE]['post']
        request = operation['requestBody']
        self.assertFalse(request['required'])
        schema = request['content']['application/json']['schema']
        self.assertEqual(schema, {'$ref': '#/components/schemas/HoldCreateRequest'})
        check = validator(schema)
        self.assertTrue(check.is_valid({}))
        invalid_bodies: tuple[object, ...] = (
            None, [], '', 1, {'userId': 'alice'}, {'quantity': 1},
            {'seat': 'A1'}, {'ttl': 90}, {'expiresAt': 'tomorrow'},
        )
        for value in invalid_bodies:
            with self.subTest(value=value):
                self.assertFalse(check.is_valid(value))
        key = next(p for p in operation['parameters'] if p['in'] == 'header')
        self.assertEqual(key['name'], 'Idempotency-Key')
        self.assertTrue(key['required'])
        self.assertFalse(validator(key['schema']).is_valid(''))
        response = resolve(operation['responses']['201']['$ref'])
        receipt = response['content']['application/json']['examples']['created']['value']
        check = validator(response['content']['application/json']['schema'])
        check.validate(receipt)
        self.assertEqual(receipt['status'], 'held')
        self.assertNotIn('secondsRemaining', receipt)
        for field in ('id', 'eventId', 'status', 'createdAt', 'expiresAt'):
            with self.subTest(missing=field):
                self.assertFalse(check.is_valid({k: v for k, v in receipt.items() if k != field}))
        check.validate({**receipt, 'futureField': True})

    def test_current_view_exposes_effective_state_countdown_and_confirmation_link(self):
        """GET represents current lifecycle state rather than replayed creation."""
        operation = SPEC['paths']['/holds/{holdId}']['get']
        response = resolve(operation['responses']['200']['$ref'])
        schema = response['content']['application/json']['schema']
        check = validator(schema)
        identity = {
            'id': 'hold_1234567890ab4def81234567890abcde', 'eventId': 'ev_103',
            'createdAt': '2026-11-08T20:00:00Z', 'expiresAt': '2026-11-08T20:01:30Z',
        }
        for status, seconds in (('held', 1), ('confirmed', 0), ('released', 0), ('expired', 0)):
            body = {**identity, 'status': status, 'secondsRemaining': seconds}
            if status == 'confirmed':
                self.assertFalse(check.is_valid(body))
                body['reservationId'] = 'res_1'
            with self.subTest(status=status):
                check.validate({**body, 'futureField': True})
                self.assertFalse(check.is_valid({k: v for k, v in body.items() if k != 'secondsRemaining'}))
                self.assertFalse(check.is_valid({**body, 'secondsRemaining': -1}))
                if status != 'held':
                    self.assertFalse(check.is_valid({**body, 'secondsRemaining': 1}))
        hold_id = validator({'$ref': '#/components/schemas/HoldId'})
        hold_id.validate(identity['id'])
        for value in ('hold_1', 'res_1', identity['id'].upper(), identity['id'] + '\n'):
            self.assertFalse(hold_id.is_valid(value))

    def test_terminal_operations_preserve_reservation_shape_and_release_recovery(self):
        """Confirmation and release expose every accepted status and recovery code."""
        confirm = SPEC['paths']['/holds/{holdId}/confirm']['post']
        release = SPEC['paths']['/holds/{holdId}']['delete']
        self.assertEqual(set(confirm['responses']), {'200', '201', '404', '409', '422', '503'})
        self.assertEqual(set(release['responses']), {'204', '404', '409', '422', '503'})
        for operation in (confirm, release):
            self.assertNotIn('requestBody', operation)
            self.assertEqual([(p['in'], p['name']) for p in operation['parameters']],
                             [('path', 'holdId')])
        for status in ('200', '201'):
            response = resolve(confirm['responses'][status]['$ref'])
            self.assertEqual(response['content']['application/json']['schema'],
                             {'$ref': '#/components/schemas/Reservation'})
        conflict = resolve(confirm['responses']['409']['$ref'])
        examples = conflict['content']['application/json']['examples']
        self.assertEqual({e['value']['error'] for e in examples.values()},
                         {'hold_expired', 'hold_released'})
        conflict = resolve(release['responses']['409']['$ref'])
        content = conflict['content']['application/json']
        body = content['example']
        self.assertEqual(body['error'], 'already_confirmed')
        validator(content['schema']).validate({**body, 'futureField': True})
        self.assertFalse(validator(content['schema']).is_valid({'error': 'already_confirmed'}))
        self.assertNotIn('content', resolve(release['responses']['204']['$ref']))

    def test_every_operation_that_can_return_503_lists_it(self):
        """Exhausted abort retries give 503, and each operation that gives it documents it."""
        examples = {'eventId': 'ev_101', 'holdId': 'hold_' + '0' * 32,
                    'reservationId': 'res_1', 'entryId': 'entry_1'}
        unavailable = set()
        with patch.object(storage, 'BOOKING_COMMANDS', AlwaysAborting(storage.BOOKING_COMMANDS)), \
                patch.object(app.logger, 'disabled', True):
            client = app.test_client()
            for path, operations in SPEC['paths'].items():
                url = path.format(**examples)
                for method, operation in operations.items():
                    status = client.open(url, method=method.upper(),
                                         headers={'Idempotency-Key': 'k'}).status_code
                    if status == 503:
                        unavailable.add(operation['operationId'])
                        with self.subTest(operation=operation['operationId']):
                            self.assertEqual(operation['responses'].get('503'),
                                             {'$ref': '#/components/responses/ServiceUnavailable'})
        self.assertEqual(unavailable, {'createReservation', 'joinWaitlist', 'createHold',
                                       'confirmHold', 'releaseHold'})


class AlwaysAborting:
    """Booking adapter whose every unit of work aborts, as if PostgreSQL kept reporting 40001."""

    def __init__(self, inner):
        self.inner = inner

    @contextmanager
    def unit_of_work(self):
        raise TransactionAborted
        yield  # pragma: no cover

    def snapshot(self):
        return self.inner.snapshot()

    def now(self):
        return self.inner.now()


if __name__ == '__main__':
    unittest.main()
