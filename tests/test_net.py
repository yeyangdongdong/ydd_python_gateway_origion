"""代理头 IP 还原：只信任回环对端。"""

import unittest

from gateway.net import forwarded_ip, ip_allowed


class ForwardedIpTests(unittest.TestCase):
    def test_trusts_forwarded_for_only_from_loopback(self):
        self.assertEqual(forwarded_ip("127.0.0.1", "192.168.1.20, 127.0.0.1"), "192.168.1.20")
        self.assertEqual(forwarded_ip("::1", None, "192.168.1.21"), "192.168.1.21")
        self.assertEqual(forwarded_ip("192.168.1.50", "10.0.0.1"), "192.168.1.50")
        self.assertEqual(forwarded_ip("127.0.0.1"), "127.0.0.1")


class IpAllowedTests(unittest.TestCase):
    def test_empty_allowlist_means_unlimited(self):
        self.assertTrue(ip_allowed("192.168.1.5", []))
        self.assertTrue(ip_allowed("not-an-ip", []))
        self.assertFalse(ip_allowed("not-an-ip", ["10.0.0.0/8"]))

    def test_ipv4_and_ipv6(self):
        self.assertTrue(ip_allowed("10.1.2.3", ["10.0.0.0/8"]))
        self.assertFalse(ip_allowed("192.168.1.5", ["10.0.0.0/8"]))
        self.assertTrue(ip_allowed("2001:db8::1", ["2001:db8::/32"]))
        self.assertTrue(ip_allowed("::ffff:10.1.2.3", ["10.0.0.0/8"]))


if __name__ == "__main__":
    unittest.main()
