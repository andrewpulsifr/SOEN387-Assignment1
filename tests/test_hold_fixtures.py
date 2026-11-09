"""Check fresh-setup data against the captured original event expectations."""

import ast
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures'


class HoldFixtureTests(unittest.TestCase):
    def test_explicit_capacity_preserves_original_availability_without_booking_history(self):
        """Fresh Capacity explains seatsLeft without inventing Reservations."""
        fixture = json.loads((ROOT / 'contracts/fixtures/fresh-events.json').read_text())
        baseline = json.loads((FIXTURES / 'original-event-responses.json').read_text())
        events = baseline['responses']['/events']['body']['events']
        self.assertEqual(
            [(event['id'], event['title'], event['capacity']) for event in fixture['events']],
            [(event['id'], event['title'], event['seatsLeft']) for event in events],
        )
        self.assertEqual([event['capacity'] for event in fixture['events']], [42, 0, 2])
        for collection in ('reservations', 'holds', 'waitlist', 'idempotencyOutcomes'):
            self.assertEqual(fixture[collection], [])
        self.assertEqual(fixture['nextReservationNumber'], 1)

    def test_original_four_event_check_bodies_are_unchanged(self):
        """Preserve supplied checks byte for byte rather than rewriting expectations."""
        baseline = json.loads((FIXTURES / 'original-event-checks.json').read_text())
        source = (ROOT / 'tests/conformance.py').read_text()
        current = {node.name: ast.get_source_segment(source, node)
                   for node in ast.parse(source).body
                   if isinstance(node, ast.FunctionDef) and node.name in baseline['checks']}
        self.assertEqual(len(current), 4)
        self.assertEqual(current, baseline['checks'])


if __name__ == '__main__':
    unittest.main()
