import time
import unittest
from unittest.mock import patch

from app import model_discovery as discovery
from app.config import DISCOVERY_MAX_AGE_HOURS, ModelConfig


class DiscoveredCatalogTests(unittest.TestCase):
    def setUp(self):
        self.original_cache = discovery._cache

    def tearDown(self):
        discovery._cache = self.original_cache

    @staticmethod
    def _cache(updated_at: float):
        return {
            "maj": updated_at,
            "modeles": [
                {
                    "provider": "groq",
                    "model_id": "groq/active-model",
                    "context": 128000,
                    "nom": "Active model",
                    "note": 80,
                    "categories": ["general", "raisonnement"],
                }
            ],
            "echecs": {},
            "pauses": {},
        }

    def test_fresh_catalog_removes_retired_curated_models_for_that_provider(self):
        discovery._cache = self._cache(time.time())
        curated = [
            ModelConfig("groq", "groq/retired-model", ["general"], priority=1),
            ModelConfig("gemini", "gemini/curated-fallback", ["general"], priority=2),
        ]

        with (
            patch.object(discovery, "MODELS", curated),
            patch.object(discovery, "DISCOVERY_ENABLED", True),
            patch.object(discovery, "PROVIDER_ORDER", ["groq", "gemini", "openrouter"]),
        ):
            result = discovery.candidats("general", mode="curated")

        keys = {(model.provider, model.model_id) for model in result}
        self.assertIn(("groq", "groq/active-model"), keys)
        self.assertNotIn(("groq", "groq/retired-model"), keys)
        # A provider without a discovered catalogue keeps its curated fallback.
        self.assertIn(("gemini", "gemini/curated-fallback"), keys)

    def test_expired_catalog_does_not_prune_curated_models_or_add_stale_models(self):
        old = time.time() - (DISCOVERY_MAX_AGE_HOURS * 3600 + 60)
        discovery._cache = self._cache(old)
        curated = [ModelConfig("groq", "groq/retired-model", ["general"], priority=1)]

        with (
            patch.object(discovery, "MODELS", curated),
            patch.object(discovery, "DISCOVERY_ENABLED", True),
        ):
            result = discovery.candidats("general", mode="curated")

        keys = {(model.provider, model.model_id) for model in result}
        self.assertIn(("groq", "groq/retired-model"), keys)
        self.assertNotIn(("groq", "groq/active-model"), keys)

    def test_disabled_discovery_ignores_cached_dynamic_models(self):
        discovery._cache = self._cache(time.time())
        curated = [ModelConfig("groq", "groq/curated-model", ["general"], priority=1)]

        with (
            patch.object(discovery, "MODELS", curated),
            patch.object(discovery, "DISCOVERY_ENABLED", False),
        ):
            result = discovery.candidats("general", mode="curated")

        keys = {(model.provider, model.model_id) for model in result}
        self.assertIn(("groq", "groq/curated-model"), keys)
        self.assertNotIn(("groq", "groq/active-model"), keys)


if __name__ == "__main__":
    unittest.main()
