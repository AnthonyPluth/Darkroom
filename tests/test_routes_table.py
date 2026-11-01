import unittest

from darkroom.server import routes
from tests.test_every_route import PUBLIC


class RoutesTableTests(unittest.TestCase):
    def test_every_route_declares_a_known_auth_kind(self):
        self.assertTrue(routes.ROUTES)
        for route in routes.ROUTES:
            self.assertIn(route.auth, routes.AUTH_KINDS, f"{route.method} {route.path}")

    def test_routes_are_unique(self):
        keys = [(route.method, route.path) for route in routes.ROUTES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_only_the_listed_routes_are_public(self):
        public = {f"{route.method} {route.path}" for route in routes.ROUTES if route.auth == "public"}
        self.assertEqual(public, PUBLIC)

    def test_find(self):
        self.assertIsNotNone(routes.find("GET", "/healthz"))
        self.assertIsNone(routes.find("POST", "/healthz"))
        self.assertIsNotNone(routes.find("POST", "/auth/logout"))
        self.assertIsNone(routes.find("GET", "/auth/logout"))


if __name__ == "__main__":
    unittest.main()
