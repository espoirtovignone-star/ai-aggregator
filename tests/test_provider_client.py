import os
import unittest
from unittest.mock import patch

from app.providers.client import chat_completions_url


class ProviderEndpointTests(unittest.TestCase):
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

    def test_sambanova_endpoint(self):
        self.assertEqual(
            chat_completions_url("sambanova"),
            "https://api.sambanova.ai/v1/chat/completions",
        )


if __name__ == "__main__":
    unittest.main()
