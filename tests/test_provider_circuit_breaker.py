import unittest
from unittest.mock import patch

import app.model_discovery as discovery
from app.config import ModelConfig
from app.providers.client import ProviderError, _parse_chat_completion


class ProviderCircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.cache = {
            "maj": 0, "modeles": [], "echecs": {}, "pauses": {},
            "pannes_provider": {},
        }
        self.cache_patch = patch.object(discovery, "_cache", self.cache)
        self.save_patch = patch.object(discovery, "_sauver_cache", lambda: None)
        self.cache_patch.start()
        self.save_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.addCleanup(self.save_patch.stop)

    def test_provider_circuit_opens_after_two_upstream_failures(self):
        self.assertEqual(discovery.signaler_provider_echec("gemini", "HTTP 503"), 0)
        self.assertEqual(discovery.signaler_provider_echec("gemini", "HTTP 503"), 90)
        self.assertTrue(discovery.provider_en_pause("gemini"))
        self.assertFalse(discovery.provider_en_pause("groq"))

    def test_provider_circuit_backoff_increases(self):
        discovery.signaler_provider_echec("openrouter", "timeout")
        self.assertEqual(discovery.signaler_provider_echec("openrouter", "timeout"), 90)
        self.assertEqual(discovery.signaler_provider_echec("openrouter", "timeout"), 180)


    def test_live_catalog_removes_stale_curated_models(self):
        self.cache["modeles"] = [{
            "provider": "groq",
            "model_id": "groq/current-free-model",
            "categories": ["general"],
            "note": 70,
        }]
        with patch.object(discovery, "MODELS", [
            ModelConfig("groq", "groq/retired-model", ["general"], priority=1),
        ]):
            selected = discovery.candidats("general", "curated")

        self.assertEqual(
            [(model.provider, model.model_id) for model in selected],
            [("groq", "groq/current-free-model")],
        )


    def test_empty_provider_response_is_an_error_not_a_success(self):
        with self.assertRaises(ProviderError):
            _parse_chat_completion("groq", 200, {"choices": []})

    def test_malformed_provider_response_is_an_error_not_a_crash(self):
        with self.assertRaises(ProviderError):
            _parse_chat_completion("gemini", 200, {"choices": [{"message": {}}]})

    def test_valid_provider_response_is_normalized(self):
        content, usage = _parse_chat_completion("groq", 200, {
            "choices": [{"message": {"content": "  Réponse correcte  "}}],
            "usage": {"prompt_tokens": "12", "completion_tokens": 8},
        })
        self.assertEqual(content, "Réponse correcte")
        self.assertEqual(usage, {"input_tokens": 12, "output_tokens": 8})

    def test_groq_allowlist_excludes_retired_and_unverified_models(self):
        import app.model_discovery as discovery
        self.assertEqual(discovery._GROQ_FREE_MODELS, {
            "openai/gpt-oss-20b",
            "openai/gpt-oss-120b",
            "qwen/qwen3.8-27b",
        })

    def test_success_closes_provider_circuit(self):
        self.cache["echecs"]["gemini/gemma-test"] = {"n": 3, "t": 1}
        self.cache["pauses"]["gemini"] = 9999999999
        self.cache["pannes_provider"]["gemini"] = {"n": 2, "t": 1}

        discovery.signaler_succes("gemini", "gemma-test")

        self.assertFalse(discovery.provider_en_pause("gemini"))
        self.assertNotIn("gemini", self.cache["pannes_provider"])
        self.assertNotIn("gemini/gemma-test", self.cache["echecs"])


if __name__ == "__main__":
    unittest.main()
