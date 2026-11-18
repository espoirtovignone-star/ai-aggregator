import os
import unittest
from unittest.mock import patch

import httpx

from app.config import MODELS, PROVIDERS, MAX_MONTHLY_SPEND_USD
from app.providers.client import ProviderError, call_model, chat_completions_url


class ProviderEndpointTests(unittest.TestCase):
    def test_default_premium_budget_is_disabled(self):
        self.assertEqual(MAX_MONTHLY_SPEND_USD, 0.0)

    def test_cloudflare_endpoint_uses_account_id_and_v1_prefix(self):
        with patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": "account-test"}):
            self.assertEqual(
                chat_completions_url("cloudflare"),
                "https://api.cloudflare.com/client/v4/accounts/account-test/ai/v1/chat/completions",
            )

    def test_cloudflare_endpoint_fails_closed_without_account_id(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLOUDFLARE_ACCOUNT_ID", None)
            with self.assertRaisesRegex(RuntimeError, "CLOUDFLARE_ACCOUNT_ID"):
                chat_completions_url("cloudflare")

    def test_metered_providers_are_not_marked_free(self):
        self.assertFalse(PROVIDERS["cloudflare"].is_free)
        self.assertFalse(PROVIDERS["sambanova"].is_free)

    def test_curated_models_exist_for_both_providers(self):
        models = {(model.provider, model.model_id) for model in MODELS}
        self.assertIn(("cloudflare", "@cf/meta/llama-3.1-8b-instruct-fast"), models)
        self.assertIn(("sambanova", "Meta-Llama-3.3-70B-Instruct"), models)

    def test_sambanova_endpoint(self):
        self.assertEqual(
            chat_completions_url("sambanova"),
            "https://api.sambanova.ai/v1/chat/completions",
        )

    def test_nvidia_nim_endpoint(self):
        self.assertEqual(
            chat_completions_url("nvidia"),
            "https://integrate.api.nvidia.com/v1/chat/completions",
        )

    def test_nvidia_is_not_automatically_free(self):
        self.assertFalse(PROVIDERS["nvidia"].is_free)

    def test_conditional_providers_are_disabled_without_free_tier_confirmation(self):
        self.assertFalse(PROVIDERS["cloudflare"].is_free)
        self.assertFalse(PROVIDERS["sambanova"].is_free)

    def test_billing_and_quota_errors_trigger_provider_fallback(self):
        from app.router import _est_rate_limit
        from app.providers.client import ProviderError
        for status in (402, 429):
            self.assertTrue(_est_rate_limit(ProviderError("cloudflare", status, "denied")))
        # HTTP 403 is handled separately as an authentication/permission pause,
        # not mislabeled as an exhausted quota.
        self.assertFalse(_est_rate_limit(ProviderError("cloudflare", 403, "denied")))
        self.assertTrue(_est_rate_limit(ProviderError("sambanova", 400, "insufficient credits")))

    def test_metered_providers_are_excluded_from_automatic_candidates(self):
        from app.model_discovery import candidats
        providers = {model.provider for model in candidats("general", mode="curated")}
        self.assertNotIn("cloudflare", providers)
        self.assertNotIn("sambanova", providers)


class PremiumProviderGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloudflare_cannot_be_called_through_premium_path(self):
        from app.router import call_premium
        with self.assertRaisesRegex(ValueError, "Seuls les providers premium"):
            await call_premium("test", "cloudflare", "any-model")

    async def test_sambanova_cannot_be_called_through_premium_path(self):
        from app.router import call_premium
        with self.assertRaisesRegex(ValueError, "Seuls les providers premium"):
            await call_premium("test", "sambanova", "any-model")

    async def test_nvidia_is_disabled_without_explicit_opt_in(self):
        from app.router import call_premium, BudgetExceeded
        with patch.dict(os.environ, {"NVIDIA_ENABLED": "false"}):
            with self.assertRaisesRegex(BudgetExceeded, "NVIDIA est désactivé"):
                await call_premium("test", "nvidia", "some-model")


class ProviderResponseValidationTests(unittest.IsolatedAsyncioTestCase):
    async def _call_with_response(self, body: bytes, status_code: int = 200):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(status_code, content=body, headers={"content-type": "application/json"})
        )
        client = httpx.AsyncClient(transport=transport)
        with (
            patch("app.providers.client.get_api_key", return_value="test-api-key"),
            patch("app.providers.client.httpx.AsyncClient", return_value=client),
        ):
            return await call_model("groq", "test-model", "private student prompt")

    async def test_valid_openai_compatible_response_returns_text_and_usage(self):
        body = b'{"choices":[{"message":{"content":"  Reponse correcte  "}}],"usage":{"prompt_tokens":12,"completion_tokens":34}}'
        result = await self._call_with_response(body)
        self.assertEqual(result, ("Reponse correcte", {"input_tokens": 12, "output_tokens": 34}))

    async def test_malformed_json_becomes_provider_error(self):
        with self.assertRaisesRegex(ProviderError, "JSON invalide"):
            await self._call_with_response(b"not-json")

    async def test_http_200_error_payload_is_classified_for_fallback(self):
        body = b'{"error":{"type":"rate_limit_error","message":"private prompt echoed"}}'
        with self.assertRaises(ProviderError) as raised:
            await self._call_with_response(body)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertNotIn("private prompt", str(raised.exception))

    async def test_missing_message_or_content_becomes_provider_error(self):
        for body in (
            b'{"choices":[{}]}',
            b'{"choices":[{"message":{}}]}',
            b'{"choices":[{"message":{"content":""}}]}',
            b'{"choices":[]}',
        ):
            with self.subTest(body=body):
                with self.assertRaises(ProviderError):
                    await self._call_with_response(body)

    async def test_multiblock_text_content_is_joined(self):
        body = b'{"choices":[{"message":{"content":[{"type":"text","text":"Partie 1"},{"type":"text","text":"Partie 2"}]}}]}'
        result = await self._call_with_response(body)
        self.assertEqual(result[0], "Partie 1\\nPartie 2")

    async def test_http_error_body_is_never_exposed(self):
        with self.assertRaises(ProviderError) as raised:
            await self._call_with_response(b'{"error":"private student prompt"}', status_code=503)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn("private student prompt", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
