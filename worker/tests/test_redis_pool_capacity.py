import unittest

from pydantic import ValidationError

from app.config import Settings


class RedisPoolCapacityTests(unittest.TestCase):
    def test_default_has_capacity_for_isolated_lanes(self):
        configured = Settings(_env_file=None)
        self.assertEqual(configured.redis_max_connections, 64)
        self.assertEqual(configured.redis_socket_timeout_seconds, 15.0)
        self.assertEqual(configured.redis_connect_timeout_seconds, 5.0)

    def test_too_small_pool_fails_fast(self):
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                redis_max_connections=63,
            )

    def test_socket_timeout_must_exceed_blocking_read_window(self):
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                redis_socket_timeout_seconds=5,
            )


if __name__ == "__main__":
    unittest.main()
