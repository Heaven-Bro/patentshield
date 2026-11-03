from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from config.health import _database_ready, _redis_url_ready


class HealthEndpointTests(SimpleTestCase):
    def test_liveness_is_independent_of_database_and_redis(self) -> None:
        with patch("config.health._database_ready", side_effect=AssertionError):
            with patch("config.health._redis_dependencies_ready", side_effect=AssertionError):
                response = self.client.get("/health/live/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "alive"})

    def test_readiness_returns_ok_only_when_all_dependencies_are_ready(self) -> None:
        with patch("config.health._database_ready", return_value=True) as database:
            with patch(
                "config.health._redis_dependencies_ready", return_value=True
            ) as redis:
                response = self.client.get("/health/ready/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "ready",
                "checks": {"database": "ok", "redis": "ok"},
            },
        )
        database.assert_called_once_with()
        redis.assert_called_once_with()

    def test_readiness_failure_is_generic_and_does_not_disclose_exceptions(self) -> None:
        with patch("config.health._database_ready", return_value=False):
            with patch("config.health._redis_dependencies_ready", return_value=False):
                response = self.client.get("/health/ready/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {
                "status": "unavailable",
                "checks": {
                    "database": "unavailable",
                    "redis": "unavailable",
                },
            },
        )
        self.assertNotIn("traceback", response.content.decode().casefold())
        self.assertNotIn("password", response.content.decode().casefold())

    def test_database_probe_uses_bounded_connection_and_statement_timeouts(self) -> None:
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = (1,)
        probe_connection = MagicMock()
        probe_connection.cursor.return_value = cursor

        with patch(
            "config.health.connection.get_connection_params",
            return_value={"host": "db.internal", "options": "-c application_name=probe"},
        ):
            with patch(
                "config.health.connection.Database.connect",
                return_value=probe_connection,
            ) as connect:
                self.assertTrue(_database_ready())

        kwargs = connect.call_args.kwargs
        self.assertEqual(kwargs["connect_timeout"], 2)
        self.assertIn("statement_timeout=1500ms", kwargs["options"])
        self.assertIn("application_name=probe", kwargs["options"])
        cursor.execute.assert_called_once_with("SELECT 1")
        probe_connection.close.assert_called_once_with()

    def test_redis_probe_uses_bounded_socket_timeouts(self) -> None:
        client = MagicMock()
        client.ping.return_value = True
        with patch("redis.Redis.from_url", return_value=client) as from_url:
            self.assertTrue(_redis_url_ready("redis://cache.internal/1"))
        self.assertEqual(
            from_url.call_args.kwargs,
            {"socket_connect_timeout": 1, "socket_timeout": 1},
        )
        client.ping.assert_called_once_with()
        client.close.assert_called_once_with()
