import unittest
from unittest.mock import patch

from app.control.proxy import ProxyDirectory


class _FakeConfig:
    def __init__(self, values: dict):
        self._values = values

    def get_str(self, key: str, default: str = "") -> str:
        return str(self._values.get(key, default))

    def get_list(self, key: str, default=None) -> list:
        value = self._values.get(key, default if default is not None else [])
        return list(value or [])

    def get_int(self, key: str, default: int = 0) -> int:
        return int(self._values.get(key, default))


def _config(**overrides):
    values = {
        "proxy.egress.mode": "single_proxy",
        "proxy.egress.proxy_url": "socks5://warp:40000",
        "proxy.egress.resource_proxy_url": "",
        "proxy.egress.proxy_pool": [],
        "proxy.egress.resource_proxy_pool": [],
        "proxy.egress.proxy_hosts": [],
        "proxy.egress.direct_hosts": [],
        "proxy.clearance.mode": "none",
        "proxy.clearance.flaresolverr_url": "",
        "proxy.clearance.timeout_sec": 60,
    }
    values.update(overrides)
    return _FakeConfig(values)


class ProxyHostRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def _directory(self, cfg: _FakeConfig) -> ProxyDirectory:
        directory = ProxyDirectory()
        with patch("app.control.proxy.get_config", return_value=cfg):
            await directory.load()
        return directory

    async def test_empty_host_routes_keep_global_proxy_behavior(self):
        directory = await self._directory(_config())

        lease = await directory.acquire()

        self.assertEqual(lease.proxy_url, "socks5://warp:40000")
        self.assertEqual(lease.clearance_host, "grok.com")

    async def test_proxy_hosts_limit_proxy_to_matching_host(self):
        directory = await self._directory(
            _config(**{"proxy.egress.proxy_hosts": ["console.x.ai"]})
        )

        grok_lease = await directory.acquire()
        console_lease = await directory.acquire(clearance_origin="https://console.x.ai")

        self.assertIsNone(grok_lease.proxy_url)
        self.assertEqual(console_lease.proxy_url, "socks5://warp:40000")

    async def test_direct_hosts_override_global_proxy(self):
        directory = await self._directory(
            _config(**{"proxy.egress.direct_hosts": ["grok.com"]})
        )

        grok_lease = await directory.acquire(clearance_origin="https://grok.com")
        console_lease = await directory.acquire(clearance_origin="https://console.x.ai")

        self.assertIsNone(grok_lease.proxy_url)
        self.assertEqual(console_lease.proxy_url, "socks5://warp:40000")

    async def test_wildcard_proxy_host_matches_subdomain(self):
        directory = await self._directory(
            _config(**{"proxy.egress.proxy_hosts": ["*.x.ai"]})
        )

        console_lease = await directory.acquire(clearance_origin="https://console.x.ai")
        apex_lease = await directory.acquire(clearance_origin="https://x.ai")

        self.assertEqual(console_lease.proxy_url, "socks5://warp:40000")
        self.assertIsNone(apex_lease.proxy_url)


if __name__ == "__main__":
    unittest.main()
