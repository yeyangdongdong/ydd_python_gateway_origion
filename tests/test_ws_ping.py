"""WebSocket 协议层 ping 配置。"""

import unittest

from gateway.config import Settings


class WsPingSettingsTests(unittest.TestCase):
    def test_defaults(self):
        settings = Settings(provider="demo", model="demo")
        self.assertEqual(settings.ws_ping_interval, 20.0)
        self.assertEqual(settings.ws_ping_timeout, 20.0)

    def test_custom_and_zero(self):
        settings = Settings(provider="demo", model="demo",
                            ws_ping_interval=5, ws_ping_timeout=0)
        self.assertEqual(settings.ws_ping_interval, 5)
        self.assertEqual(settings.ws_ping_timeout, 0)

    def test_negative_values_are_rejected(self):
        with self.assertRaises(ValueError):
            Settings(provider="demo", model="demo", ws_ping_interval=-1)
        with self.assertRaises(ValueError):
            Settings(provider="demo", model="demo", ws_ping_timeout=-1)


if __name__ == "__main__":
    unittest.main()
