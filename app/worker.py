"""Worker durable Kofi : traite les tâches persistées, sans bloquer l'API HTTP."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any

from app.math_verifier import verify_math_answer

from app.durable_queue import (
    DurableKofiQueue,
    QueueConfigurationError,
    QueueSettings,
)

logger = logging.getLogger(__name__)


def build_queue_from_environment() -> DurableKofiQueue:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise QueueConfigurationError(
            "SUPABASE_URL et SUPABASE_SERVICE_ROLE_KEY sont requis pour le worker."
        )
    return DurableKofiQueue(
        QueueSettings(
            url=url,
            service_role_key=key,
            timeout_seconds=float(os.environ.get("KOFI_QUEUE_HTTP_TIMEOUT", "8")),
        )
    )


def _worker_id() -> str:
    # Identifiant stable par instance, sans exposer de secret.
    suffix = os.environ.get("RENDER_INSTANCE_ID") or socket.gethostname()
    return f"kofi-{suffix}"[:128]


async def process_job(queue: DurableKofiQueue, job: dict[str, Any]) -> None:
    job_id = str(job.get("id") or "")
    lease_token = str(job.get("lease_token") or "")
    payload = job.get("payload")
    if not job_id or not lease_token or not isinstance(payload, dict):
        logger.error("Tâche Kofi mal formée (identifiant masqué).")
        return

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        await queue.retry(job_id, lease_token, 1, "invalid_job_payload")
        return

    try:
        # Import tardif : le serveur API peut démarrer même si la file est désactivée.
        from app.router import classify_prompt, pick_and_call

        category = payload.get("category")
        if not isinstance(category, str) or not category.strip():
            category = await classify_prompt(prompt)
        provider, model_id, answer = await pick_and_call(
            prompt, category, quota_queue=queue
        )
        if payload.get("verify_math") is True:
            findings = verify_math_answer(answer)
            if findings:
                correction_prompt = (
                    f"{prompt}\n\n# CORRECTION MATHÉMATIQUE OBLIGATOIRE\n"
                    "Le contrôle indépendant a relevé les points suivants :\n"
                    + "\n".join(f"- {finding}" for finding in findings)
                    + "\nReprends l'exercice depuis l'énoncé original. Corrige chaque erreur confirmée, "
                    "vérifie les calculs et fournis la solution finale complète.\n\n"
                    f"BROUILLON À CONTRÔLER :\n{answer}"
                )
                provider, model_id, answer = await pick_and_call(
                    correction_prompt, category, quota_queue=queue
                )
                remaining_findings = verify_math_answer(answer)
                if remaining_findings:
                    raise RuntimeError(
                        "math_verification_failed: "
                        + " | ".join(remaining_findings[:3])
                    )
        completed = await queue.complete(
            job_id,
            lease_token,
            {
                "response": answer,
                "provider": provider,
                "model": model_id,
                "category": category,
            },
        )
        if not completed:
            logger.warning("Résultat Kofi non enregistré : bail expiré ou remplacé.")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        attempts = int(job.get("attempts") or 1)
        # Backoff plafonné : 15s, 30s, 60s, 120s...
        delay = min(900, 15 * (2 ** min(max(attempts - 1, 0), 6)))
        code = "provider_unavailable"
        try:
            from app.router import NoModelAvailable
            if isinstance(exc, NoModelAvailable):
                code = "providers_exhausted"
        except Exception:
            pass
        try:
            status = await queue.retry(job_id, lease_token, delay, code)
            logger.warning("Tâche Kofi différée (%s), prochaine tentative dans %ss.", status, delay)
        except Exception:
            # Le bail expirera et un autre worker pourra reprendre la tâche.
            logger.exception("Impossible de reprogrammer une tâche Kofi.")


async def run_worker(queue: DurableKofiQueue, stop: asyncio.Event) -> None:
    worker_id = _worker_id()
    concurrency = max(1, min(3, int(os.environ.get("KOFI_QUEUE_CONCURRENCY", "1"))))
    poll_seconds = max(1.0, min(15.0, float(os.environ.get("KOFI_QUEUE_POLL_SECONDS", "2"))))
    logger.info("Worker Kofi démarré (concurrence=%s).", concurrency)
    loop = asyncio.get_running_loop()
    last_cleanup = loop.time() - 3600  # Nettoyage immédiat au démarrage, puis toutes les heures.

    while not stop.is_set():
        if loop.time() - last_cleanup >= 3600:
            try:
                deleted = await queue.cleanup(retention_days=14, limit=1000)
                if deleted:
                    logger.info("Nettoyage Kofi : %s tâches terminées/échouées supprimées (>14 jours).", deleted)
            except Exception:
                # Un échec de nettoyage ne doit jamais interrompre le traitement des exercices.
                logger.exception("Nettoyage périodique de la file Kofi impossible.")
            last_cleanup = loop.time()

        try:
            jobs = await queue.claim(
                worker_id,
                limit=concurrency,
                lease_seconds=180,
            )
            if jobs:
                await asyncio.gather(*(process_job(queue, job) for job in jobs))
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Lecture de la file Kofi impossible; nouvelle tentative.")
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass
    logger.info("Worker Kofi arrêté.")


def worker_enabled() -> bool:
    return os.environ.get("KOFI_QUEUE_WORKER_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "oui"
    )
