"""路由清单：拆分后关键路径仍存在，且没有重复注册。"""

import tempfile
import unittest
from pathlib import Path

from gateway.app import create_app
from gateway.config import Settings


def flatten(routes):
    """FastAPI 的 include_router 会保留 _IncludedRouter，这里递归展开它的原 router。"""
    for route in routes:
        nested = getattr(route, "routes", None)
        if nested is None and hasattr(route, "original_router"):
            nested = route.original_router.routes
        if nested:
            yield from flatten(nested)
        elif hasattr(route, "path"):
            yield route


class RouteInventoryTests(unittest.TestCase):
    def test_key_paths_are_registered_once(self):
        with tempfile.TemporaryDirectory() as prefix:
            root = Path(prefix)
            app = create_app(Settings(provider="demo", model="demo",
                                      db_path=root / "g.sqlite3",
                                      routes_path=root / "no-routes.json"))
        routes = list(flatten(app.routes))
        paths = {route.path for route in routes}
        expected = {
            "/", "/health", "/login", "/logout", "/auth/status", "/auth/password",
            "/sessions", "/sessions/{session_id}", "/sessions/{session_id}/chat",
            "/sessions/{session_id}/stream", "/sessions/{session_id}/messages",
            "/sessions/{session_id}/ticket", "/sessions/{session_id}/requests/{request_id}",
            "/agents", "/agents/{agent_id}", "/runs/{run_id}",
            "/users", "/users/{user_id}", "/api-keys", "/api-keys/{key_id}",
            "/audit", "/proxy-status", "/proxy-ticket", "/metrics",
        }
        self.assertTrue(expected <= paths, expected - paths)

        registered = [(route.path, method)
                      for route in routes
                      for method in getattr(route, "methods", ()) or ()]
        duplicates = {pair for pair in registered if registered.count(pair) > 1}
        self.assertEqual(duplicates, set())


if __name__ == "__main__":
    unittest.main()
