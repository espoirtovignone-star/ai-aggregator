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
    def __init__(self, provider: str, status_code: int, detail: str):
        self.provider = provider
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"[{provider}] HTTP {status_code}: {detail}")


def _provider_error_status(data: dict, fallback_status: int) -> int:
    """Classe un refus fournisseur sans conserver son message potentiellement sensible."""
    error = data.get("error")
    if isinstance(error, dict):
        raw = " ".join(
            str(error.get(key, ""))
            for key in ("code", "type", "message")
            if error.get(key) is not None
        )
    else:
        raw = str(error or "")
    normalized = raw.lower()
    if any(marker in normalized for marker in ("rate limit", "rate_limit", "quota", "too many")):
        return 429
    if any(marker in normalized for marker in ("billing", "insufficient credit", "payment required", "insufficient balance")):
        return 402
    return fallback_status


def _extract_text(content: object) -> str:
    """Accepte le contenu texte standard et les blocs texte OpenAI multimodaux."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            part["text"].strip()
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip()
        ]
        return "\n".join(parts)
    return ""


def _token_count(usage: dict, key: str) -> int:
    value = usage.get(key, 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


async def call_model(
    provider: str, model_id: str, prompt: str, system: str | None = None
) -> tuple[str, dict]:
    """
    Appelle un modèle via l'API OpenAI-compatible du provider donné.
    Retourne (contenu_texte, usage) où usage = {"input_tokens": int, "output_tokens": int}.
    Les réponses distantes malformées sont converties en ProviderError afin que
    le routeur puisse poursuivre sa cascade au lieu d'interrompre toute la réponse.
    """
    api_key = get_api_key(provider)

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    url = chat_completions_url(provider)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model_id, "messages": messages}
    # Anthropic exige max_tokens explicitement via sa couche de compat OpenAI
    if provider == "anthropic":
        payload["max_tokens"] = 4096

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async with asyncio.timeout(TOTAL_REQUEST_TIMEOUT_SECONDS):
            resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code != 200:
        # Ne pas journaliser le corps brut : certains fournisseurs peuvent y
        # recopier une partie du prompt de l'élève.
        raise ProviderError(provider, resp.status_code, "Réponse HTTP non valide du fournisseur.")

    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        raise ProviderError(provider, resp.status_code, "Réponse JSON invalide du fournisseur.") from exc

    if not isinstance(data, dict):
        raise ProviderError(provider, resp.status_code, "Format JSON inattendu du fournisseur.")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        status = _provider_error_status(data, resp.status_code)
        raise ProviderError(provider, status, "Le fournisseur n'a renvoyé aucun choix exploitable.")

    first_choice = choices[0]
    message = first_choice.get("message") if isinstance(first_choice, dict) else None
    content = _extract_text(message.get("content")) if isinstance(message, dict) else ""
    if not content:
        raise ProviderError(provider, resp.status_code, "Le fournisseur a renvoyé un contenu vide ou invalide.")

    raw_usage = data.get("usage")
    if not isinstance(raw_usage, dict):
        raw_usage = {}
    usage = {
        "input_tokens": _token_count(raw_usage, "prompt_tokens"),
        "output_tokens": _token_count(raw_usage, "completion_tokens"),
    }
    return content, usage
