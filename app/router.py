"""
Coeur de l'agrégateur :
1. classify_prompt() : un petit modèle rapide tague le prompt (code, redaction, etc.)
2. pick_and_call()   : on prend le MEILLEUR modèle de cette catégorie qui a
                        encore du quota dispo, avec fallback en cascade.
                        Un modèle précis peut aussi être forcé manuellement.

Depuis la découverte automatique (app/model_discovery.py), la liste des
candidats n'est plus figée : elle mélange les modèles écrits à la main dans
config.py et ceux repérés en direct chez les providers, triés par note de
qualité. Un modèle qui plante 3 fois de suite est écarté 6 h automatiquement.
"""
import asyncio
import logging
import httpx
import os
import time

from app.config import MODELS, ModelConfig, PROVIDERS, PROVIDER_ORDER, MODEL_SELECTION_MODE
from app.providers.client import call_model, ProviderError
from app.quota import can_use, record_call
from app.local_classifier import classify_local
from app.budget import can_afford, record_spend, estimate_cost_usd
from app.model_discovery import (
    candidats,
    est_sur_liste_noire,
    modeles_decouverts,
    provider_en_pause,
    signaler_echec,
    signaler_rate_limit,
    signaler_provider_pause,
    signaler_succes,
)

# Budget total de la cascade, compatible avec le délai d'attente de Kofi.
# Chaque appel reçoit uniquement le temps restant : un fournisseur lent ne peut
# donc pas faire dépasser le délai global avant le passage au message de secours.
DELAI_MAX_CASCADE = 28  # secondes
# Une tentative lente ne doit pas monopoliser toute la cascade : on garde
# du temps pour essayer les autres providers gratuits.
DELAI_MAX_PAR_TENTATIVE = 6  # secondes
DELAI_MAX_PAR_PROVIDER = 8  # secondes cumulées par fournisseur


def _est_rate_limit(e: ProviderError) -> bool:
    """Détecte quota, crédit ou accès payant : on met le compte en pause."""
    code = getattr(e, "status_code", None)
    if code in (402, 429):
        return True
    texte = str(getattr(e, "detail", "") or e).lower()
    indicateurs = (
        "rate limit", "rate-limit", "too many requests", "quota",
        "payment required", "billing", "insufficient credit",
        "insufficient balance", "payment method", "free tier",
        "upgrade required", "exceeded your allowance",
    )
    return any(mot in texte for mot in indicateurs)

logger = logging.getLogger(__name__)


class NoModelAvailable(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class InvalidModel(Exception):
    pass


async def classify_prompt(prompt: str) -> str:
    """Classification locale par règles pondérées — aucun appel réseau, latence ~0."""
    return classify_local(prompt)


def _candidates_for(category: str, mode: str | None = None) -> list[ModelConfig]:
    """
    Modèles à essayer pour cette catégorie, du meilleur au moins bon.
    Toute la logique de tri et de fusion est dans app/model_discovery.py.
    `mode` permet de forcer "qualite" le temps d'un seul appel.
    """
    return candidats(category, mode)


def _modele_connu(provider: str, model_id: str) -> bool:
    """Le modèle existe-t-il, soit dans config.py, soit parmi ceux découverts ?"""
    if any(m.provider == provider and m.model_id == model_id for m in MODELS):
        return True
    return any(m.provider == provider and m.model_id == model_id for m in modeles_decouverts())


async def _try_forced_model(
    prompt: str, provider: str, model_id: str, estimated_total: int,
    timeout_seconds: float,
) -> tuple[str, str, str] | None:
    """
    Essaie le modèle gratuit demandé explicitement par l'utilisateur.
    Renvoie None (et laisse pick_and_call retomber sur la cascade normale) si
    le quota est épuisé ou si l'appel échoue. Lève InvalidModel si le
    provider/modèle demandé n'existe pas ou n'est pas gratuit — ça, c'est une
    erreur de saisie côté appelant, pas une histoire de quota, donc ça remonte.
    """
    if provider not in PROVIDERS or not PROVIDERS[provider].is_free:
        raise InvalidModel(f"'{provider}' n'est pas un provider gratuit valide.")
    if not _modele_connu(provider, model_id):
        raise InvalidModel(
            f"Modèle {provider}/{model_id} inconnu (ni dans config.py, ni parmi les modèles découverts). "
            f"Appelle GET /models/discover?refresh=true pour rafraîchir le catalogue."
        )

    if not can_use(provider, estimated_tokens=estimated_total):
        logger.info("Quota épuisé pour le modèle forcé %s/%s, bascule sur la cascade normale", provider, model_id)
        return None
    try:
        content, usage = await asyncio.wait_for(
            call_model(provider, model_id, prompt),
            timeout=max(0.001, min(timeout_seconds, DELAI_MAX_PAR_TENTATIVE)),
        )
        record_call(provider, tokens=usage["input_tokens"] + usage["output_tokens"])
        signaler_succes(provider, model_id)
        return provider, model_id, content
    except (ProviderError, RuntimeError, httpx.HTTPError, TimeoutError) as e:
        if isinstance(e, ProviderError) and _est_rate_limit(e):
            signaler_rate_limit(provider, str(getattr(e, "detail", "")))
        elif isinstance(e, ProviderError) and e.status_code in (401, 403):
            signaler_provider_pause(provider, 1800, str(getattr(e, "detail", "")))
        elif isinstance(e, ProviderError) and e.status_code in (400, 404, 422):
            signaler_echec(provider, model_id, immediat=True)
        else:
            signaler_echec(provider, model_id)
        logger.warning("Echec du modèle forcé %s/%s: %s, bascule sur la cascade normale", provider, model_id, e)
        return None


async def pick_and_call(
    prompt: str,
    category: str,
    forced_provider: str | None = None,
    forced_model_id: str | None = None,
    mode: str | None = None,
    quota_queue=None,
) -> tuple[str, str, str]:
    """
    Essaie les modèles gratuits de la catégorie dans l'ordre de priorité, en
    sautant ceux dont le quota est épuisé, puis retombe sur les modèles 'general'.
    Ne touche JAMAIS aux providers payants (filtrés explicitement).

    Si forced_provider/forced_model_id sont fournis, ce modèle précis est
    essayé en premier ; en cas de quota épuisé ou d'erreur, on retombe sur la
    cascade normale (comportement "meilleur effort") plutôt que d'échouer.
    Retourne (provider, model_id, réponse).
    """
    estimated_input = len(prompt) // 4
    estimated_total = estimated_input + 500

    async def reserve_shared_quota(provider: str, model_id: str) -> bool:
        if quota_queue is None:
            return True
        cfg = PROVIDERS[provider]
        try:
            allowed = await quota_queue.reserve_provider_quota(
                provider=provider,
                model_id=model_id,
                rpm=cfg.rpm,
                rpd=cfg.rpd,
                tpm=cfg.tpm,
                estimated_tokens=estimated_total,
            )
        except Exception:
            # Mode file durable : échec de la base de quota = refus prudent.
            logger.exception("Réservation du quota partagé impossible pour %s/%s", provider, model_id)
            return False
        if not allowed:
            logger.info("Quota partagé atteint pour %s/%s", provider, model_id)
        return allowed

    debut = time.monotonic()
    provider_elapsed: dict[str, float] = {}
    if forced_provider and forced_model_id:
        remaining = DELAI_MAX_CASCADE - (time.monotonic() - debut)
        forced_started = time.monotonic()
        if remaining > 0:
            forced_result = await _try_forced_model(
                prompt, forced_provider, forced_model_id, estimated_total,
                min(remaining, DELAI_MAX_PAR_PROVIDER),
            )
            provider_elapsed[forced_provider] = time.monotonic() - forced_started
            if forced_result is not None:
                return forced_result

    tried = set()
    if forced_provider and forced_model_id:
        tried.add((forced_provider, forced_model_id))

    candidate_lists = [_candidates_for(category, mode)]
    if category != "general":
        candidate_lists.append(_candidates_for("general", mode))

    logger.info(
        "[Routage] catégorie=%s mode=%s ordre_providers=%s candidats=%s",
        category,
        mode or MODEL_SELECTION_MODE,
        PROVIDER_ORDER,
        [[f"{m.provider}/{m.model_id}" for m in group] for group in candidate_lists],
    )

    for candidates in candidate_lists:
        for m in candidates:
            if not PROVIDERS[m.provider].is_free:
                continue  # sécurité : le routage auto ne doit jamais choisir un provider payant
            key = (m.provider, m.model_id)
            if key in tried:
                continue
            tried.add(key)
            provider_remaining = DELAI_MAX_PAR_PROVIDER - provider_elapsed.get(m.provider, 0.0)
            if provider_remaining <= 0:
                logger.info("[Routage] budget temps fournisseur épuisé: %s", m.provider)
                continue
            if not can_use(m.provider, estimated_tokens=estimated_total):
                logger.info("Quota local insuffisant pour %s/%s, on saute", m.provider, m.model_id)
                continue
            if est_sur_liste_noire(m.provider, m.model_id):
                logger.info("[Routage] modèle ignoré (liste noire): %s/%s", m.provider, m.model_id)
                continue
            # Provider mis en pause par un 429 : inutile d'essayer ses autres
            # modèles, c'est le compte entier qui est plafonné.
            if provider_en_pause(m.provider):
                logger.info("[Routage] provider ignoré (en pause): %s", m.provider)
                continue
            # Ne réserver le quota qu'une fois le modèle éligible, juste avant l'appel.
            if not await reserve_shared_quota(m.provider, m.model_id):
                continue
            if time.monotonic() - debut > DELAI_MAX_CASCADE:
                logger.warning("Cascade interrompue après %ss, on rend la main", DELAI_MAX_CASCADE)
                raise NoModelAvailable(
                    "Aucun modèle gratuit n'a répondu à temps. Les providers sont saturés, "
                    "réessaie dans quelques minutes."
                )
            tentative_started = time.monotonic()
            try:
                remaining = DELAI_MAX_CASCADE - (time.monotonic() - debut)
                if remaining <= 0:
                    raise NoModelAvailable(
                        "Le délai global de la cascade gratuite est écoulé. Réessaie dans quelques instants."
                    )
                content, usage = await asyncio.wait_for(
                    call_model(m.provider, m.model_id, prompt),
                    timeout=min(remaining, provider_remaining, DELAI_MAX_PAR_TENTATIVE),
                )
                record_call(m.provider, tokens=usage["input_tokens"] + usage["output_tokens"])
                signaler_succes(m.provider, m.model_id)
                logger.info("Succès %s/%s en %.2fs", m.provider, m.model_id, time.monotonic() - debut)
                return m.provider, m.model_id, content
            except (ProviderError, RuntimeError, httpx.HTTPError, TimeoutError) as e:
                if isinstance(e, ProviderError) and _est_rate_limit(e):
                    # Tout le provider est plafonné : on le met en pause, la
                    # boucle sautera ses autres modèles dès le tour suivant.
                    signaler_rate_limit(m.provider, str(getattr(e, "detail", "")))
                elif isinstance(e, ProviderError) and e.status_code in (401, 403):
                    signaler_provider_pause(m.provider, 1800, str(getattr(e, "detail", "")))
                elif isinstance(e, ProviderError) and e.status_code in (400, 404, 422):
                    # Un identifiant rejeté ne doit pas être retenté à chaque élève.
                    signaler_echec(m.provider, m.model_id, immediat=True)
                else:
                    # Les erreurs réseau/serveur restent temporaires : 3 échecs
                    # consécutifs suffisent avant une mise à l'écart de 6 h.
                    signaler_echec(m.provider, m.model_id)
                # On compte quand même l'appel : un 429 consomme du quota chez
                # la plupart des providers, et sans ça le compteur local ne
                # voyait jamais qu'on était à sec.
                try:
                    record_call(m.provider, tokens=0)
                except Exception:
                    pass
                logger.warning("Echec %s/%s: %s", m.provider, m.model_id, e)
                continue
            finally:
                provider_elapsed[m.provider] = provider_elapsed.get(m.provider, 0.0) + (
                    time.monotonic() - tentative_started
                )

    raise NoModelAvailable(
        "Tous les providers gratuits sont soit à quota épuisé, soit en erreur. Réessaie dans une minute."
    )


async def call_premium(prompt: str, provider: str, model_id: str) -> tuple[str, dict]:
    """
    Appel explicite à un provider payant (anthropic/openai). Ne jamais appeler
    automatiquement depuis pick_and_call — uniquement sur demande explicite de l'utilisateur.
    Vérifie le budget mensuel AVANT l'appel (estimation) et enregistre le coût réel APRÈS.
    """
    if provider == "nvidia":
        if os.environ.get("NVIDIA_ENABLED", "false").strip().lower() not in ("1", "true", "yes", "oui"):
            raise BudgetExceeded("NVIDIA est désactivé. Active NVIDIA_ENABLED=true seulement après vérification des conditions tarifaires de ton compte.")
        if not model_id:
            raise ValueError("Définis NVIDIA_MODEL_ID avec un modèle disponible dans ton catalogue NVIDIA.")
        if not can_use("nvidia"):
            raise BudgetExceeded("Limite locale NVIDIA atteinte (RPM/RPD).")
        content, usage = await call_model(provider, model_id, prompt)
        record_call(provider, tokens=usage["input_tokens"] + usage["output_tokens"])
        # NVIDIA ne publie pas un tarif unique valable pour tous les modèles :
        # ne pas afficher un coût nul trompeur ni prétendre plafonner la facture.
        return content, {**usage, "cost_usd": None}

    if provider not in ("anthropic", "openai"):
        raise ValueError("Seuls les providers premium anthropic, openai et NVIDIA explicitement activé sont autorisés ici.")

    rough_estimate = estimate_cost_usd(provider, input_tokens=len(prompt) // 4, output_tokens=500)
    if not can_afford(rough_estimate):
        raise BudgetExceeded(
            f"Plafond mensuel atteint pour les providers payants. "
            f"Augmente MAX_MONTHLY_SPEND_USD dans .env si besoin."
        )

    content, usage = await call_model(provider, model_id, prompt)
    real_cost = estimate_cost_usd(provider, usage["input_tokens"], usage["output_tokens"])
    record_spend(real_cost)

    return content, {**usage, "cost_usd": round(real_cost, 6)}
