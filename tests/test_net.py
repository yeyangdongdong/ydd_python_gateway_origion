"""代理头 IP 还原：只信任回环对端。"""

import unittest

from gateway.net import forwarded_ip


class ForwardedIpTests(unittest.TestCase):
    def test_trusts_forwarded_for_only_from_loopback(self):
        self.assertEqual(forwarded_ip("127.0.0.1", "192.168.1.20, 127.0.0.1"), "192.168.1.20")
        self.assertEqual(forwarded_ip("::1", None, "192.168.1.21"), "192.168.1.21")
        self.assertEqual(forwarded_ip("192.168.1.50", "10.0.0.1"), "192.168.1.50")
        self.assertEqual(forwarded_ip("127.0.0.1"), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
