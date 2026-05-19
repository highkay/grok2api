import base64
import unittest

from app.control.proxy.models import ProxyFeedbackKind
from app.dataplane.reverse.transport import asset_upload


class FakeResponse:
    status_code = 200
    content = b"image-bytes"
    headers = {"content-type": "image/png; charset=binary"}


class FakeProxy:
    def __init__(self):
        self.feedback_events = []

    async def acquire(self):
        return "lease-1"

    async def feedback(self, lease, feedback):
        self.feedback_events.append((lease, feedback.kind, feedback.status_code))


class FakeSession:
    last_get_headers = None
    last_get_url = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url, *, headers, timeout):
        self.__class__.last_get_url = url
        self.__class__.last_get_headers = headers
        self.__class__.last_get_timeout = timeout
        return FakeResponse()


class AssetUploadSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_fetch_does_not_send_grok_auth_headers(self):
        fake_proxy = FakeProxy()
        uploaded = {}

        async def fake_get_proxy_runtime():
            return fake_proxy

        async def fake_upload_file(token, filename, mime, b64):
            uploaded.update(
                {
                    "token": token,
                    "filename": filename,
                    "mime": mime,
                    "b64": b64,
                }
            )
            return "file-id", "file-uri"

        original_get_proxy_runtime = asset_upload.get_proxy_runtime
        original_session = asset_upload.ResettableSession
        original_build_session_kwargs = asset_upload.build_session_kwargs
        original_upload_file = asset_upload.upload_file
        try:
            asset_upload.get_proxy_runtime = fake_get_proxy_runtime
            asset_upload.ResettableSession = FakeSession
            asset_upload.build_session_kwargs = lambda *, lease: {"lease": lease}
            asset_upload.upload_file = fake_upload_file

            result = await asset_upload.upload_from_input(
                "secret-grok-token",
                "https://example.test/path/image.png?download=1",
            )
        finally:
            asset_upload.get_proxy_runtime = original_get_proxy_runtime
            asset_upload.ResettableSession = original_session
            asset_upload.build_session_kwargs = original_build_session_kwargs
            asset_upload.upload_file = original_upload_file

        self.assertEqual(result, ("file-id", "file-uri"))
        self.assertEqual(FakeSession.last_get_url, "https://example.test/path/image.png?download=1")
        self.assertEqual(FakeSession.last_get_headers, asset_upload._URL_FETCH_HEADERS)
        self.assertNotIn("Cookie", FakeSession.last_get_headers)
        self.assertNotIn("Authorization", FakeSession.last_get_headers)
        self.assertNotIn("X-Xai-Auth", FakeSession.last_get_headers)
        self.assertEqual(uploaded["token"], "secret-grok-token")
        self.assertEqual(uploaded["filename"], "image.png")
        self.assertEqual(uploaded["mime"], "image/png")
        self.assertEqual(uploaded["b64"], base64.b64encode(b"image-bytes").decode())
        self.assertIn(("lease-1", ProxyFeedbackKind.SUCCESS, None), fake_proxy.feedback_events)


if __name__ == "__main__":
    unittest.main()
