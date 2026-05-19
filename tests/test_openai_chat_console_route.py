import unittest
from unittest.mock import patch

from app.products.openai import chat


class _FakeProxy:
    def __init__(self):
        self.acquire_calls = []

    async def acquire(self, **kwargs):
        self.acquire_calls.append(kwargs)
        return object()


class _FakeResponse:
    status_code = 200
    content = b"{}"


class _FakeSession:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.posts = []
        self.closed = False
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True

    async def post(self, *args, **kwargs):
        self.posts.append((args, kwargs))
        return _FakeResponse()


class ConsoleRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_console_post_acquires_console_clearance(self):
        proxy = _FakeProxy()

        async def get_proxy_runtime():
            return proxy

        _FakeSession.instances.clear()
        with (
            patch.object(chat, "get_proxy_runtime", get_proxy_runtime),
            patch.object(chat, "ResettableSession", _FakeSession),
            patch.object(chat, "build_session_kwargs", return_value={}),
            patch.object(chat, "build_http_headers", return_value={"x-test": "1"}) as headers,
        ):
            session, _response = await chat._console_post(
                token="token",
                console_model="grok-4.3",
                input=[{"role": "user", "content": "ping"}],
                instructions="",
                stream=False,
                temperature=0.7,
                top_p=0.95,
                tools=None,
                tool_choice=None,
                timeout_s=1.0,
            )

        self.assertEqual(proxy.acquire_calls, [{"clearance_origin": chat.CONSOLE_BASE}])
        headers.assert_called_once()
        self.assertEqual(headers.call_args.kwargs["origin"], chat.CONSOLE_BASE)
        self.assertEqual(headers.call_args.kwargs["referer"], f"{chat.CONSOLE_BASE}/")
        self.assertEqual(len(_FakeSession.instances), 1)
        self.assertFalse(session.closed)
        await session.__aexit__(None, None, None)
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
