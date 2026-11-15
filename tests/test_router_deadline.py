import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import router


class CascadeDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_forced_model_and_fallback_share_one_deadline(self):
        async def slow_call(*args, **kwargs):
            await asyncio.sleep(1)
            return "late", {"input_tokens": 1, "output_tokens": 1}

        candidate = SimpleNamespace(provider="groq", model_id="test-model")
        with (
            patch.object(router, "DELAI_MAX_CASCADE", 0.08),
            patch.object(router, "_modele_connu", return_value=True),
            patch.object(router, "_candidates_for", return_value=[]),
            patch.object(router, "can_use", return_value=True),
            patch.object(router, "call_model", side_effect=slow_call),
            patch.object(router, "signaler_echec"),
        ):
            started = time.monotonic()
            with self.assertRaises(router.NoModelAvailable):
                await router.pick_and_call(
                    "test", "general",
                    forced_provider="groq",
                    forced_model_id=candidate.model_id,
                )
            self.assertLess(time.monotonic() - started, 0.35)

    async def test_provider_error_falls_through_to_next_free_candidate(self):
        candidates = [
            SimpleNamespace(provider="groq", model_id="first"),
            SimpleNamespace(provider="groq", model_id="second"),
        ]
        calls = []

        async def call_model(provider, model_id, prompt):
            calls.append(model_id)
            if model_id == "first":
                raise RuntimeError("temporary upstream failure")
            return "answer", {"input_tokens": 3, "output_tokens": 4}

        with (
            patch.object(router, "DELAI_MAX_CASCADE", 1.0),
            patch.object(router, "_candidates_for", return_value=candidates),
            patch.object(router, "can_use", return_value=True),
            patch.object(router, "est_sur_liste_noire", return_value=False),
            patch.object(router, "provider_en_pause", return_value=False),
            patch.object(router, "call_model", side_effect=call_model),
            patch.object(router, "record_call"),
            patch.object(router, "signaler_echec"),
            patch.object(router, "signaler_succes"),
        ):
            result = await router.pick_and_call("test", "general")

        self.assertEqual(result, ("groq", "second", "answer"))
        self.assertEqual(calls, ["first", "second"])


if __name__ == "__main__":
    unittest.main()
