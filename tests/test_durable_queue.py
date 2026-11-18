import json
import unittest

import httpx

from app.durable_queue import (
    DurableKofiQueue,
    QueueConfigurationError,
    QueueUnavailable,
    QueueSettings,
)


class DurableQueueTests(unittest.IsolatedAsyncioTestCase):
    def make_queue(self, handler):
        transport = httpx.MockTransport(handler)
        settings = QueueSettings(
            url="https://example.supabase.co",
            service_role_key="server-only-test-key",
        )
        return DurableKofiQueue(settings, transport=transport)

    async def test_claim_uses_private_service_role_rpc_and_returns_jobs(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["headers"] = request.headers
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=[{"id": "job-1", "status": "processing"}])

        jobs = await self.make_queue(handler).claim("worker-test-01", limit=3)

        self.assertEqual(jobs, [{"id": "job-1", "status": "processing"}])
        self.assertEqual(
            seen["url"],
            "https://example.supabase.co/rest/v1/rpc/claim_kofi_jobs",
        )
        self.assertEqual(seen["headers"]["apikey"], "server-only-test-key")
        self.assertEqual(
            seen["headers"]["authorization"],
            "Bearer server-only-test-key",
        )
        self.assertEqual(
            seen["body"],
            {
                "p_worker_id": "worker-test-01",
                "p_limit": 3,
                "p_lease_seconds": 180,
            },
        )

    async def test_complete_retry_and_quota_rpc_contracts(self):
        calls = []

        def handler(request):
            calls.append((request.url.path, json.loads(request.content)))
            if request.url.path.endswith("/complete_kofi_job"):
                return httpx.Response(200, json=True)
            if request.url.path.endswith("/retry_kofi_job"):
                return httpx.Response(200, json="retrying")
            if request.url.path.endswith("/cleanup_kofi_queue"):
                return httpx.Response(200, json=7)
            return httpx.Response(200, json=False)

        queue = self.make_queue(handler)
        self.assertTrue(await queue.complete("job-1", "lease-1", {"text": "OK"}))
        self.assertEqual(
            await queue.retry("job-1", "lease-1", 30, "provider_busy"),
            "retrying",
        )
        self.assertFalse(
            await queue.reserve_provider_quota(
                "groq", "model-x", 5, 100, 6000, 900
            )
        )
        self.assertEqual(await queue.cleanup(retention_days=14, limit=500), 7)

        self.assertEqual(
            [path.rsplit("/", 1)[-1] for path, _ in calls],
            [
                "complete_kofi_job",
                "retry_kofi_job",
                "reserve_kofi_provider_quota",
                "cleanup_kofi_queue",
            ],
        )
        self.assertEqual(calls[1][1]["p_public_error_code"], "provider_busy")

    async def test_upstream_failure_is_sanitized(self):
        queue = self.make_queue(
            lambda request: httpx.Response(
                503, text="internal secret database detail"
            )
        )
        with self.assertRaisesRegex(QueueUnavailable, "HTTP 503") as exc:
            await queue.claim("worker-test")
        self.assertNotIn("internal secret", str(exc.exception))

    async def test_claim_rejects_invalid_worker_parameters(self):
        queue = self.make_queue(
            lambda request: httpx.Response(200, json=[])
        )
        with self.assertRaises(ValueError):
            await queue.claim("x")
        with self.assertRaises(ValueError):
            await queue.claim("worker-test", limit=11)

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(QueueConfigurationError):
            QueueSettings(url="not-a-url", service_role_key="secret")
        with self.assertRaises(QueueConfigurationError):
            QueueSettings(
                url="https://example.supabase.co",
                service_role_key=" ",
            )


if __name__ == "__main__":
    unittest.main()
