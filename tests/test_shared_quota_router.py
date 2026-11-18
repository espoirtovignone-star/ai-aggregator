import unittest
from unittest.mock import AsyncMock, patch

from app.config import PROVIDERS, ModelConfig
from app.router import NoModelAvailable, pick_and_call


class FakeQuotaQueue:
    def __init__(self, allowed: bool):
        self.allowed = allowed
        self.calls = []

    async def reserve_provider_quota(self, **kwargs):
        self.calls.append(kwargs)
        return self.allowed


class SharedQuotaRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_skips_provider_when_shared_quota_is_exhausted(self):
        queue = FakeQuotaQueue(allowed=False)
        candidate = ModelConfig("groq", "model-test", ["general"], priority=1)

        with (
            patch("app.router._candidates_for", return_value=[candidate]),
            patch("app.router.can_use", return_value=True),
            patch("app.router.est_sur_liste_noire", return_value=False),
            patch("app.router.provider_en_pause", return_value=False),
            patch("app.router.call_model", new=AsyncMock()) as call_model,
        ):
            with self.assertRaises(NoModelAvailable):
                await pick_and_call("Résous 2x = 8", "general", quota_queue=queue)

        self.assertEqual(len(queue.calls), 1)
        self.assertEqual(queue.calls[0]["provider"], "groq")
        self.assertEqual(queue.calls[0]["model_id"], "model-test")
        self.assertEqual(queue.calls[0]["rpm"], PROVIDERS["groq"].rpm)
        self.assertEqual(queue.calls[0]["rpd"], PROVIDERS["groq"].rpd)
        self.assertEqual(queue.calls[0]["tpm"], PROVIDERS["groq"].tpm)
        call_model.assert_not_awaited()

    async def test_worker_reserves_shared_quota_before_calling_provider(self):
        queue = FakeQuotaQueue(allowed=True)
        candidate = ModelConfig("groq", "model-test", ["general"], priority=1)

        with (
            patch("app.router._candidates_for", return_value=[candidate]),
            patch("app.router.can_use", return_value=True),
            patch("app.router.est_sur_liste_noire", return_value=False),
            patch("app.router.provider_en_pause", return_value=False),
            patch(
                "app.router.call_model",
                new=AsyncMock(return_value=("Réponse", {"input_tokens": 12, "output_tokens": 8})),
            ) as call_model,
            patch("app.router.record_call"),
            patch("app.router.signaler_succes"),
        ):
            result = await pick_and_call(
                "Résous 2x = 8", "general", quota_queue=queue
            )

        self.assertEqual(result, ("groq", "model-test", "Réponse"))
        self.assertEqual(len(queue.calls), 1)
        call_model.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
