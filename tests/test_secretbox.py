import unittest

from darkroom.storage import secretbox


class SecretboxTests(unittest.TestCase):
    def test_round_trip(self):
        sealed = secretbox.seal("immich-key-123", "secret")
        self.assertTrue(sealed.startswith("enc:v1:"))
        self.assertNotIn("immich-key-123", sealed)
        self.assertEqual(secretbox.unseal(sealed, "secret"), "immich-key-123")

    def test_each_seal_differs(self):
        self.assertNotEqual(secretbox.seal("x", "secret"), secretbox.seal("x", "secret"))

    def test_the_wrong_key_or_a_plain_value_is_unreadable(self):
        sealed = secretbox.seal("x", "secret")
        with self.assertRaises(secretbox.Unreadable):
            secretbox.unseal(sealed, "other")
        with self.assertRaises(secretbox.Unreadable):
            secretbox.unseal("plain", "secret")
        with self.assertRaises(secretbox.Unreadable):
            secretbox.seal("x", "")

    def test_is_sealed(self):
        self.assertTrue(secretbox.is_sealed(secretbox.seal("x", "k")))
        self.assertFalse(secretbox.is_sealed("x"))


if __name__ == "__main__":
    unittest.main()
