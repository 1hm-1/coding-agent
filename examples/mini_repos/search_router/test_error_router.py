import unittest

from error_router import response_status


class ErrorRouterTest(unittest.TestCase):
    def test_archive_limit_is_rate_limited(self):
        self.assertEqual(response_status("ARCHIVE_LIMIT_REACHED"), 429)

    def test_other_routes_are_unchanged(self):
        self.assertEqual(response_status("ACCOUNT_LOCKED"), 423)
        self.assertEqual(response_status("UPSTREAM_TIMEOUT"), 504)


if __name__ == "__main__":
    unittest.main()
