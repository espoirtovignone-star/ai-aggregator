"""
Client unifié : Groq, Gemini et OpenRouter parlent tous le protocole
OpenAI-compatible (/chat/completions). Un seul client suffit, on change
juste base_url + api_key + model selon le provider choisi par le router.
"""
import asyncio
import os

import httpx

from app.config import PROVIDERS, get_api_key

TIMEOUT = httpx.Timeout(18.0, connect=5.0)
TOTAL_REQUEST_TIMEOUT_SECONDS = 18.0


def chat_completions_url(provider: str) -> str:
    """Construit l'endpoint OpenAI-compatible du provider."""
    cfg = PROVIDERS[provider]
    if provider == "cloudflare":
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        if not account_id:
            raise RuntimeError("CLOUDFLARE_ACCOUNT_ID est manquant.")
        return f"{cfg.base_url.rstrip('/')}/{account_id}/ai/v1/chat/completions"
    return cfg.base_url.rstrip("/") + "/chat/completions"


class ProviderError(Exception):
    def __init__(
        self, provider: str, status_code: int, detail: str,
        retry_after_seconds: float | None = None,
    ):
        self.provider = provider
        self.status_code = status_code
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"[{provider}] HTTP {status_code}: {detail}")


def _parse_chat_completion(provider: str, status_code: int, data: object) -> tuple[str, dict]:
    """Valide la structure OpenAI-compatible et refuse toute réponse vide."""
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
        detail = data.get("error") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            detail = detail.get("message", str(data)[:500])
        elif not detail:
            detail = str(data)[:500]
        raise ProviderError(provider, status_code, str(detail)[:500])

    choice = data["choices"][0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ProviderError(provider, status_code, "Réponse vide ou contenu absent")

    raw_usage = data.get("usage", {})
    if not isinstance(raw_usage, dict):
        raw_usage = {}
    try:
        usage = {
            "input_tokens": max(0, int(raw_usage.get("prompt_tokens", 0) or 0)),
            "output_tokens": max(0, int(raw_usage.get("completion_tokens", 0) or 0)),
        }
    except (TypeError, ValueError):
        usage = {"input_tokens": 0, "output_tokens": 0}
    return content.strip(), usage


async def call_model(
    provider: str, model_id: str, prompt: str, system: str | None = None
) -> tuple[str, dict]:
    """
    Appelle un modèle via l'API OpenAI-compatible du provider donné.
    Retourne (contenu_texte, usage) où usage = {"input_tokens": int, "output_tokens": int}.
    """
    cfg = PROVIDERS[provider]
    try:
        api_key = get_api_key(provider)
    except RuntimeError as exc:
        # Une clé absente/invalide est un problème du compte entier, pas du
        # modèle choisi : le routeur ouvrira immédiatement le circuit 401.
        raise ProviderError(provider, 401, str(exc)) from exc

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        url = chat_completions_url(provider)
    except RuntimeError as exc:
        # Une configuration manquante (ex. Account ID Cloudflare) concerne
        # tout le fournisseur : on l'écarte immédiatement de la cascade.
        raise ProviderError(provider, 401, str(exc)) from exc
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model_id, "messages": messages}
    # Anthropic exige max_tokens explicitement via sa couche de compat OpenAI
    if provider == "anthropic":
        payload["max_tokens"] = 4096

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async with asyncio.timeout(TOTAL_REQUEST_TIMEOUT_SECONDS):
            resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code != 200:
        retry_after = resp.headers.get("retry-after")
        try:
            retry_after_seconds = max(0.0, float(retry_after)) if retry_after else None
        except (TypeError, ValueError):
            retry_after_seconds = None
        raise ProviderError(
            provider, resp.status_code, resp.text[:500],
            retry_after_seconds=retry_after_seconds,
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderError(provider, resp.status_code, "Réponse JSON invalide du fournisseur") from exc
    return _parse_chat_completion(provider, resp.status_code, data)
