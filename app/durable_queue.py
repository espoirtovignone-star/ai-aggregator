"""Client serveur pour la file durable Kofi dans Supabase Postgres.

Les clés Supabase restent côté serveur. Ce module ne crée ni ne modifie le schéma :
la migration correspondante vit dans le dépôt AFROACADEMY et doit être validée
puis appliquée séparément avant activation de ce client.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx


class QueueConfigurationError(RuntimeError):
    """Configuration Supabase absente ou invalide."""


class QueueUnavailable(RuntimeError):
    """La file Supabase ne peut pas être jointe."""


@dataclass(frozen=True)
class QueueSettings:
    url: str
    service_role_key: str
    timeout_seconds: float = 8.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.url.strip())
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise QueueConfigurationError("SUPABASE_URL doit être une URL HTTP(S) valide.")
        if not self.service_role_key.strip():
            raise QueueConfigurationError("La clé Supabase serveur est manquante.")
        if not 1 <= self.timeout_seconds <= 30:
            raise QueueConfigurationError("Le délai Supabase doit être compris entre 1 et 30 secondes.")


class DurableKofiQueue:
    """Accès REST privé aux RPC de file, exclusivement avec service_role."""

    def __init__(
        self,
        settings: QueueSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._base_url = settings.url.rstrip("/") + "/rest/v1"

    async def _rpc(self, name: str, payload: dict[str, Any]) -> Any:
        headers = {
            "apikey": self._settings.service_role_key,
            "Authorization": f"Bearer {self._settings.service_role_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/rpc/{name}",
                    headers=headers,
                    json=payload,
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise QueueUnavailable("La file Kofi est momentanément inaccessible.") from exc

        if not response.is_success:
            # Ne jamais remonter le corps Supabase, qui peut contenir des détails internes.
            raise QueueUnavailable(f"Échec de la file Kofi (HTTP {response.status_code}).")
        try:
            return response.json()
        except ValueError as exc:
            raise QueueUnavailable("Réponse invalide du service de file Kofi.") from exc

    async def claim(
        self,
        worker_id: str,
        limit: int = 1,
        lease_seconds: int = 180,
    ) -> list[dict[str, Any]]:
        if not 3 <= len(worker_id.strip()) <= 128:
            raise ValueError("worker_id doit contenir entre 3 et 128 caractères.")
        if not 1 <= limit <= 10:
            raise ValueError("limit doit être compris entre 1 et 10.")
        if not 30 <= lease_seconds <= 900:
            raise ValueError("lease_seconds doit être compris entre 30 et 900.")
        data = await self._rpc(
            "claim_kofi_jobs",
            {
                "p_worker_id": worker_id,
                "p_limit": limit,
                "p_lease_seconds": lease_seconds,
            },
        )
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise QueueUnavailable("Format de tâches Kofi invalide.")
        return data

    async def complete(
        self,
        job_id: str,
        lease_token: str,
        result: dict[str, Any],
    ) -> bool:
        if not job_id or not lease_token or not isinstance(result, dict):
            raise ValueError("Identifiants de tâche ou résultat invalides.")
        data = await self._rpc(
            "complete_kofi_job",
            {
                "p_job_id": job_id,
                "p_lease_token": lease_token,
                "p_result": result,
            },
        )
        return data is True

    async def retry(
        self,
        job_id: str,
        lease_token: str,
        delay_seconds: int,
        public_error_code: str,
    ) -> str | None:
        if not job_id or not lease_token:
            raise ValueError("Identifiants de tâche invalides.")
        if not 1 <= delay_seconds <= 86400:
            raise ValueError("delay_seconds doit être compris entre 1 et 86400.")
        data = await self._rpc(
            "retry_kofi_job",
            {
                "p_job_id": job_id,
                "p_lease_token": lease_token,
                "p_delay_seconds": delay_seconds,
                "p_public_error_code": public_error_code[:80],
            },
        )
        return data if data in ("retrying", "failed") else None

    async def cleanup(self, retention_days: int = 14, limit: int = 1000) -> int:
        if not 1 <= retention_days <= 90:
            raise ValueError("retention_days doit être compris entre 1 et 90.")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit doit être compris entre 1 et 10000.")
        data = await self._rpc(
            "cleanup_kofi_queue",
            {"p_retention_days": retention_days, "p_limit": limit},
        )
        if not isinstance(data, int) or data < 0:
            raise QueueUnavailable("Résultat de nettoyage de file invalide.")
        return data

    async def reserve_provider_quota(
        self,
        provider: str,
        model_id: str,
        rpm: int,
        rpd: int,
        tpm: int | None,
        estimated_tokens: int,
    ) -> bool:
        if not provider or not model_id or rpm < 1 or rpd < 1 or estimated_tokens < 0:
            raise ValueError("Paramètres de quota invalides.")
        if tpm is not None and tpm < 1:
            raise ValueError("tpm doit être positif ou nul.")
        data = await self._rpc(
            "reserve_kofi_provider_quota",
            {
                "p_provider": provider,
                "p_model_id": model_id,
                "p_rpm": rpm,
                "p_rpd": rpd,
                "p_tpm": tpm,
                "p_estimated_tokens": estimated_tokens,
            },
        )
        return data is True
