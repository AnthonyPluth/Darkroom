import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CALLER = ROOT / ".github/workflows/merge-gate.yml"


def gate_inputs():
    workflow = CALLER.read_text()
    call = workflow[workflow.index("    uses: AnthonyPluth/fleet/.github/workflows/merge-gate.yml@"):]
    block = call[call.index("    with:\n"):]
    return dict(re.findall(r"^      ([a-z-]+): (\S.*)$", block, re.M))


class MergeGateCallerTests(unittest.TestCase):
    def test_the_gate_never_marks_a_draft_ready(self):
        self.assertEqual(gate_inputs().get("auto-ready"), "false")

    def test_the_owner_decisions_on_the_gate_stay(self):
        inputs = gate_inputs()
        self.assertEqual(inputs.get("require-codex-review"), "true")
        self.assertEqual(inputs.get("automerge-fleet-bumps"), "true")
