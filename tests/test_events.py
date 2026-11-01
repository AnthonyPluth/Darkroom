import unittest

from darkroom.domain import events
from darkroom.storage import db
from tests.shared import own_database


class EventTests(unittest.TestCase):
    def test_record_and_recent(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            events.record(opened, 1.0, "import", "jo", "added 3")
            events.record(opened, 2.0, "alert_sent", "jo")
            events.record(opened, 3.0, "import", "sam")
        with db.session(engine) as opened:
            self.assertEqual([e.subject for e in events.recent(opened)], ["sam", "jo", "jo"])
            self.assertEqual([e.detail for e in events.recent(opened, type="import")], [None, "added 3"])
            self.assertEqual(len(events.recent(opened, limit=1)), 1)


if __name__ == "__main__":
    unittest.main()
