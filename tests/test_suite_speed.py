import ast
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
LONGEST_POLL = 0.1


def serving_threads(tree: ast.AST):
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) == "Thread"):
            continue
        target = next((keyword.value for keyword in node.keywords if keyword.arg == "target"), None)
        if isinstance(target, ast.Attribute) and target.attr == "serve_forever":
            yield node


def poll_of(thread: ast.Call):
    passed = next((keyword.value for keyword in thread.keywords if keyword.arg == "kwargs"), None)
    if not isinstance(passed, ast.Dict):
        return None
    for key, value in zip(passed.keys, passed.values, strict=True):
        if isinstance(key, ast.Constant) and key.value == "poll_interval" and isinstance(value, ast.Constant):
            return value.value
    return None


class ServerShutdownTests(unittest.TestCase):
    def test_every_server_a_test_serves_in_a_thread_stops_within_a_short_poll(self):
        found = []
        for path in sorted(TESTS.rglob("*.py")):
            for thread in serving_threads(ast.parse(path.read_text(), str(path))):
                where = f"{path.relative_to(TESTS)}:{thread.lineno}"
                found.append(where)
                poll = poll_of(thread)
                with self.subTest(where=where):
                    self.assertIsNotNone(poll, "pass kwargs={'poll_interval': 0.05}: serve_forever's default 0.5 s makes every shutdown() wait")
                    self.assertLessEqual(poll, LONGEST_POLL)
        self.assertGreaterEqual(len(found), 10)

    def test_the_check_sees_a_thread_without_a_poll(self):
        slow = ast.parse("threading.Thread(target=server.serve_forever, daemon=True)")
        quick = ast.parse("threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)")
        other = ast.parse("threading.Thread(target=work, daemon=True)")
        self.assertEqual([poll_of(thread) for thread in serving_threads(slow)], [None])
        self.assertEqual([poll_of(thread) for thread in serving_threads(quick)], [0.05])
        self.assertEqual(list(serving_threads(other)), [])


if __name__ == "__main__":
    unittest.main()
