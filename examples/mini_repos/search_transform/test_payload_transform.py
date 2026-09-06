import unittest

from payload_transform import normalize_payload


class PayloadTransformTest(unittest.TestCase):
    def test_legacy_customer_id_is_normalized(self):
        self.assertEqual(
            normalize_payload({"legacy_customer_id": "customer-7", "active": True}),
            {"customer_id": "customer-7", "active": True},
        )

    def test_unrelated_payload_is_unchanged(self):
        self.assertEqual(normalize_payload({"name": "Ada"}), {"name": "Ada"})


if __name__ == "__main__":
    unittest.main()
