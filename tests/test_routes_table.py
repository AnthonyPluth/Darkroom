import unittest

from darkroom.server import routes


class RoutesTableTests(unittest.TestCase):
    def test_every_route_declares_a_known_auth_kind(self):
        self.assertTrue(routes.ROUTES)
        for route in routes.ROUTES:
            self.assertIn(route.auth, routes.AUTH_KINDS, f"{route.method} {route.path}")

    def test_routes_are_unique(self):
        keys = [(route.method, route.path) for route in routes.ROUTES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_only_healthz_is_public(self):
        public = sorted(route.path for route in routes.ROUTES if route.auth == "public")
        self.assertEqual(public, ["/healthz"])

    def test_find(self):
        self.assertIsNotNone(routes.find("GET", "/healthz"))
        self.assertIsNone(routes.find("POST", "/healthz"))


if __name__ == "__main__":
    unittest.main()
