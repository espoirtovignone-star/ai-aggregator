"""
Découverte automatique des modèles gratuits.

Pourquoi ce fichier existe
--------------------------
Avant, la liste des modèles était figée dans config.py : à chaque fois qu'un
provider sortait un nouveau modèle gratuit (et ça arrive tous les mois), il
fallait éditer le code à la main. Et à l'inverse, quand un modèle était retiré,
l'agrégateur continuait à l'appeler et se prenait des erreurs.

Ce module interroge directement les providers (Groq, OpenRouter, Gemini) pour
savoir ce qui EXISTE VRAIMENT en ce moment, ne garde que ce qui est gratuit,
note chaque modèle selon sa puissance probable, puis fusionne le tout avec la
liste "de confiance" écrite à la main dans config.py.

Résultat concret :
  - un nouveau modèle gratuit puissant apparaît chez un provider -> il est
    utilisé automatiquement, sans toucher au code ;
  - un modèle est retiré ou tombe en panne -> il est mis de côté tout seul ;
  - le classement à l'intérieur d'une catégorie se fait par NOTE de qualité,
    donc l'agrégateur appelle toujours le meilleur modèle disponible en premier.

Rien ici ne peut casser l'agrégateur : si un provider ne répond pas, on retombe
simplement sur la liste écrite à la main dans config.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import httpx

from app.config import (
    DISCOVERY_ENABLED,
    DISCOVERY_FILE,
    DISCOVERY_MAX_AGE_HOURS,
    MODELS,
    MODEL_SELECTION_MODE,
    PROVIDERS,
    ModelConfig,
)

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# ─────────────────────────────────────────────────────────────────────────────
# 1. Filtres : ce qui n'est PAS un modèle de conversation utilisable
# ─────────────────────────────────────────────────────────────────────────────

# Un provider renvoie aussi ses modèles audio, image, embeddings, garde-fous...
# Aucun ne sait répondre à un prompt texte : on les écarte d'office.
_EXCLUS = (
    "whisper", "tts", "embed", "embedding", "rerank", "moderation", "guard",
    "safety", "diffusion", "stable-", "flux", "dall-e", "imagen", "veo",
    "sora", "playai", "speech", "transcribe", "audio", "image-", "-image",
    "ocr", "clip", "bge-", "e5-", "vision-encoder",
    "lyria", "music", "video", "-vace", "wan-",
)

# Certains providers sont plus fiables que d'autres en pratique (OpenRouter
# subit régulièrement de la congestion upstream, Groq quasiment jamais). Ce
# bonus n'entre PAS dans la note du modèle : il sert uniquement à départager
# deux modèles de puissance comparable au moment de choisir lequel appeler.
# Groq a un quota gratuit très large (des milliers d'appels/jour) et ne
# limite quasiment jamais ; OpenRouter plafonne à quelques dizaines d'appels
# gratuits par jour et renvoie des 429 dès qu'on insiste. D'où l'écart.
_BONUS_FIABILITE = {"groq": 10, "gemini": 5, "openrouter": 0}

# ─────────────────────────────────────────────────────────────────────────────
# 2. Notation : à quel point ce modèle est probablement bon
# ─────────────────────────────────────────────────────────────────────────────

# Bonus par famille connue. C'est le SEUL endroit qui vieillit : une famille
# inconnue n'est pas pénalisée, elle est juste notée sur ses caractéristiques
# objectives (taille, contexte, capacité de raisonnement).
_FAMILLES = {
    "gpt-oss": 14,
    "deepseek": 14,
    "kimi": 13,
    "qwen3": 12,
    "qwen": 10,
    "nemotron": 12,
    "llama-4": 12,
    "llama-3.3": 10,
    "llama-3.1": 7,
    "gemini-3": 15,
    "gemini-2.5": 11,
    "gemini-2.0": 8,
    "mistral": 9,
    "magistral": 10,
    "devstral": 10,
    "hermes": 9,
    "gemma": 8,
    "phi": 7,
    "glm": 10,
    "minimax": 10,
    "compound": 12,
    "dots": 10,
    "grok": 11,
    "command": 9,
    "north": 9,
}

_MOTS_RAISONNEMENT = ("reasoning", "thinking", "-r1", "r1-", "reasoner", "magistral", "think")
_MOTS_CODE = ("coder", "code", "devstral", "codestral", "starcoder")


def _taille_en_milliards(model_id: str) -> float:
    """
    Extrait la taille du modèle depuis son identifiant ("llama-3.3-70b" -> 70).
    Pour les modèles MoE écrits "550b-a55b", on garde le total (550) : c'est lui
    qui reflète le mieux la connaissance embarquée.
    """
    tailles = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*b\b", model_id.lower())]
    return max(tailles) if tailles else 0.0


def noter_modele(model_id: str, contexte: int = 0) -> int:
    """
    Note de 0 à 100, purement heuristique, calculée sur des faits objectifs :
    taille du modèle, taille de la fenêtre de contexte, famille, capacité de
    raisonnement annoncée. Sert à trier les candidats du meilleur au moins bon.
    """
    mid = model_id.lower()
    note = 40

    taille = _taille_en_milliards(mid)
    if taille >= 400:
        note += 24
    elif taille >= 200:
        note += 21
    elif taille >= 100:
        note += 18
    elif taille >= 60:
        note += 14
    elif taille >= 30:
        note += 10
    elif taille >= 20:
        note += 7
    elif taille >= 10:
        note += 4
    elif taille > 0:
        note += 1

    if contexte >= 1_000_000:
        note += 12
    elif contexte >= 400_000:
        note += 9
    elif contexte >= 180_000:
        note += 7
    elif contexte >= 120_000:
        note += 5
    elif contexte >= 60_000:
        note += 2

    for famille, bonus in _FAMILLES.items():
        if famille in mid:
            note += bonus
            break

    if any(mot in mid for mot in _MOTS_RAISONNEMENT):
        note += 8
    if "instruct" in mid or "-it" in mid or "chat" in mid:
        note += 2

    # Malus : versions allégées, expérimentales ou dépréciées
    if "distill" in mid:
        note -= 6
    if "lite" in mid or "mini" in mid or "nano" in mid or "small" in mid:
        note -= 5
    if "preview" in mid or "-exp" in mid or "experimental" in mid:
        note -= 3
    if "deprecated" in mid or "legacy" in mid:
        note -= 30

    return max(0, min(100, int(note)))


def categoriser(model_id: str, contexte: int, note: int) -> list[str]:
    """Déduit à quoi sert ce modèle, à partir de son nom et de ses capacités."""
    mid = model_id.lower()
    cats: list[str] = []

    if any(mot in mid for mot in _MOTS_CODE):
        cats.append("code")
    if any(mot in mid for mot in _MOTS_RAISONNEMENT) or note >= 72:
        cats.append("raisonnement")
    if contexte >= 180_000:
        cats.append("contexte_long")
    if _taille_en_milliards(mid) and _taille_en_milliards(mid) <= 12:
        cats += ["classification", "rapide"]
    if note >= 62:
        cats += ["redaction", "creatif"]

    cats.append("general")  # tout modèle de conversation peut servir de secours
    return sorted(set(cats))


# ─────────────────────────────────────────────────────────────────────────────
# 3. Interrogation des providers
# ─────────────────────────────────────────────────────────────────────────────

def _cle(provider: str) -> str | None:
    return os.environ.get(PROVIDERS[provider].api_key_env) or None


def _utilisable(model_id: str) -> bool:
    mid = model_id.lower()
    return not any(mot in mid for mot in _EXCLUS)


async def _lister_openrouter(client: httpx.AsyncClient) -> list[dict]:
    """
    OpenRouter publie son catalogue SANS clé API, avec les tarifs.
    Un modèle est gratuit quand prompt ET completion coûtent 0.
    """
    resp = await client.get("https://openrouter.ai/api/v1/models")
    resp.raise_for_status()
    trouves = []
    for m in resp.json().get("data", []):
        mid = m.get("id", "")
        if not mid or not _utilisable(mid):
            continue
        prix = m.get("pricing") or {}
        try:
            gratuit = float(prix.get("prompt", 1)) == 0 and float(prix.get("completion", 1)) == 0
        except (TypeError, ValueError):
            gratuit = False
        if not gratuit:
            continue
        archi = m.get("architecture") or {}
        entrees = archi.get("input_modalities") or ["text"]
        sorties = archi.get("output_modalities") or ["text"]
        # On ne garde que ce qui LIT et ECRIT du texte : ça écarte d'office les
        # modèles de musique, d'image ou de vidéo qui sont eux aussi à 0 €.
        if "text" not in entrees or "text" not in sorties:
            continue
        trouves.append({
            "provider": "openrouter",
            "model_id": mid,
            "context": int(m.get("context_length") or 0),
            "nom": m.get("name") or mid,
        })
    return trouves


async def _lister_groq(client: httpx.AsyncClient) -> list[dict]:
    """Groq expose /models avec la clé API. Tout y est gratuit dans le free tier."""
    cle = _cle("groq")
    if not cle:
        return []
    resp = await client.get(
        PROVIDERS["groq"].base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {cle}"},
    )
    resp.raise_for_status()
    trouves = []
    for m in resp.json().get("data", []):
        mid = m.get("id", "")
        if not mid or not _utilisable(mid):
            continue
        if m.get("active") is False:
            continue
        trouves.append({
            "provider": "groq",
            "model_id": mid,
            "context": int(m.get("context_window") or 0),
            "nom": mid,
        })
    return trouves


async def _lister_gemini(client: httpx.AsyncClient) -> list[dict]:
    """Gemini : catalogue via l'API native, filtré sur les modèles de génération."""
    cle = _cle("gemini")
    if not cle:
        return []
    resp = await client.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": cle, "pageSize": 200},
    )
    resp.raise_for_status()
    trouves = []
    for m in resp.json().get("models", []):
        nom_complet = m.get("name", "")
        mid = nom_complet.replace("models/", "")
        if not mid or not _utilisable(mid):
            continue
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        trouves.append({
            "provider": "gemini",
            "model_id": mid,
            "context": int(m.get("inputTokenLimit") or 0),
            "nom": m.get("displayName") or mid,
        })
    return trouves


async def interroger_providers() -> list[dict]:
    """Interroge les trois providers en parallèle. Un échec n'en bloque aucun autre."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resultats = await asyncio.gather(
            _lister_groq(client),
            _lister_openrouter(client),
            _lister_gemini(client),
            return_exceptions=True,
        )

    modeles: list[dict] = []
    for res in resultats:
        if isinstance(res, Exception):
            logger.warning("[Découverte] un provider n'a pas répondu : %s", res)
            continue
        modeles.extend(res)

    for m in modeles:
        m["note"] = noter_modele(m["model_id"], m["context"])
        m["categories"] = categoriser(m["model_id"], m["context"], m["note"])
    return modeles


# ─────────────────────────────────────────────────────────────────────────────
# 4. Cache disque + liste noire des modèles qui échouent
# ─────────────────────────────────────────────────────────────────────────────

_cache: dict | None = None
DUREE_LISTE_NOIRE = 6 * 3600  # 6 h : un modèle en panne est réessayé après
SEUIL_LISTE_NOIRE = 3         # 3 échecs d'affilée avant mise à l'écart

# Quand un provider répond "rate limit exceeded", inutile d'essayer ses autres
# modèles : c'est le COMPTE qui est plafonné, pas le modèle. On met donc tout le
# provider en pause et la cascade saute directement au provider suivant. Sans ça,
# une limite OpenRouter faisait échouer 6 modèles à la suite, la requête traînait
# et le navigateur finissait en timeout.
PAUSE_RATE_LIMIT = 10 * 60      # 10 min pour une limite par minute
PAUSE_LIMITE_JOURNALIERE = 3 * 3600  # 3 h quand c'est le quota du jour qui saute


def _charger_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(DISCOVERY_FILE, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        _cache = {"maj": 0, "modeles": [], "echecs": {}, "pauses": {}}
    _cache.setdefault("echecs", {})
    _cache.setdefault("modeles", [])
    _cache.setdefault("maj", 0)
    _cache.setdefault("pauses", {})
    return _cache


def _sauver_cache() -> None:
    if _cache is None:
        return
    try:
        os.makedirs(os.path.dirname(DISCOVERY_FILE), exist_ok=True)
        with open(DISCOVERY_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        # Sur Render le disque peut être en lecture seule : ce n'est pas grave,
        # le cache reste alors en mémoire jusqu'au prochain redémarrage.
        logger.warning("[Découverte] cache non sauvegardé : %s", e)


def cache_perime() -> bool:
    cache = _charger_cache()
    if not cache["modeles"]:
        return True
    age_heures = (time.time() - cache.get("maj", 0)) / 3600
    return age_heures > DISCOVERY_MAX_AGE_HOURS


async def rafraichir(force: bool = False) -> dict:
    """Relance une découverte si le cache est périmé (ou si force=True)."""
    if not DISCOVERY_ENABLED:
        return {"actif": False, "modeles": 0, "maj": None}
    cache = _charger_cache()
    if not force and not cache_perime():
        return {"actif": True, "modeles": len(cache["modeles"]), "maj": cache["maj"], "rafraichi": False}

    modeles = await interroger_providers()
    if modeles:
        cache["modeles"] = modeles
        cache["maj"] = time.time()
        _sauver_cache()
    return {"actif": True, "modeles": len(cache["modeles"]), "maj": cache["maj"], "rafraichi": bool(modeles)}


def signaler_rate_limit(provider: str, message: str = "") -> int:
    """
    Met TOUT le provider en pause après un 429. Renvoie la durée de la pause.
    Un message évoquant une limite journalière déclenche une pause plus longue.
    """
    cache = _charger_cache()
    texte = (message or "").lower()
    journalier = any(mot in texte for mot in ("per day", "par jour", "daily", "day limit", "free-models-per-day"))
    duree = PAUSE_LIMITE_JOURNALIERE if journalier else PAUSE_RATE_LIMIT
    cache.setdefault("pauses", {})[provider] = time.time() + duree
    _sauver_cache()
    logger.warning("[Quota] %s en pause %s min (%s)", provider, duree // 60, message[:120])
    return duree


def provider_en_pause(provider: str) -> bool:
    cache = _charger_cache()
    fin = (cache.get("pauses") or {}).get(provider, 0)
    return time.time() < fin


def providers_en_pause() -> dict:
    cache = _charger_cache()
    maintenant = time.time()
    return {
        p: int(fin - maintenant)
        for p, fin in (cache.get("pauses") or {}).items()
        if fin > maintenant
    }


def signaler_echec(provider: str, model_id: str) -> None:
    """Appelé par le router quand un modèle plante : 3 fois de suite = mise à l'écart 6 h."""
    cache = _charger_cache()
    cle = f"{provider}/{model_id}"
    entree = cache["echecs"].get(cle, {"n": 0, "t": 0})
    if time.time() - entree.get("t", 0) > DUREE_LISTE_NOIRE:
        entree = {"n": 0, "t": 0}
    entree["n"] += 1
    entree["t"] = time.time()
    cache["echecs"][cle] = entree
    _sauver_cache()


def signaler_succes(provider: str, model_id: str) -> None:
    """Un appel réussi efface l'ardoise du modèle."""
    cache = _charger_cache()
    cle = f"{provider}/{model_id}"
    if cle in cache["echecs"]:
        del cache["echecs"][cle]
        _sauver_cache()


def est_sur_liste_noire(provider: str, model_id: str) -> bool:
    cache = _charger_cache()
    entree = cache["echecs"].get(f"{provider}/{model_id}")
    if not entree:
        return False
    if time.time() - entree.get("t", 0) > DUREE_LISTE_NOIRE:
        return False
    return entree.get("n", 0) >= SEUIL_LISTE_NOIRE


# ─────────────────────────────────────────────────────────────────────────────
# 5. Fusion liste écrite à la main + liste découverte
# ─────────────────────────────────────────────────────────────────────────────

def modeles_decouverts() -> list[ModelConfig]:
    cache = _charger_cache()
    sortie = []
    for m in cache.get("modeles", []):
        if m["provider"] not in PROVIDERS:
            continue
        sortie.append(ModelConfig(
            provider=m["provider"],
            model_id=m["model_id"],
            categories=m.get("categories", ["general"]),
            priority=100,  # priorité brute : c'est la note qui tranche, voir candidats()
        ))
    return sortie


def bonus_fiabilite(provider: str) -> int:
    return _BONUS_FIABILITE.get(provider, 0)


def note_de(m: ModelConfig) -> int:
    """Note d'un modèle, qu'il vienne du cache de découverte ou de config.py."""
    cache = _charger_cache()
    for d in cache.get("modeles", []):
        if d["provider"] == m.provider and d["model_id"] == m.model_id:
            return d.get("note", 0)
    return noter_modele(m.model_id)


def candidats(category: str) -> list[ModelConfig]:
    """
    Liste ordonnée des modèles à essayer pour une catégorie donnée.

    Deux modes (variable d'env MODEL_SELECTION_MODE) :
      - "qualite" (par défaut) : on trie par NOTE décroissante, donc le modèle
        le plus puissant disponible passe en premier, découvert ou pas.
      - "curated" : on garde l'ordre écrit à la main dans config.py, et les
        modèles découverts ne servent qu'en renfort derrière.

    Les catégories "classification" et "rapide" sont toujours triées à
    l'envers (le plus petit/rapide d'abord) : pour taguer un prompt, on veut
    de la vitesse, pas de la puissance.
    """
    tous = list(MODELS)
    vus = {(m.provider, m.model_id) for m in tous}
    for m in modeles_decouverts():
        if (m.provider, m.model_id) not in vus:
            tous.append(m)
            vus.add((m.provider, m.model_id))

    def utilisable(m, cat):
        return (
            cat in m.categories
            and PROVIDERS[m.provider].is_free
            and not est_sur_liste_noire(m.provider, m.model_id)
        )

    retenus = [m for m in tous if utilisable(m, category)]
    if not retenus:
        retenus = [m for m in tous if utilisable(m, "general")]

    # On écarte les providers en pause (429 récent). Si ça ne laisse plus rien,
    # on remet tout le monde : mieux vaut retenter que ne rien répondre du tout.
    sans_pause = [m for m in retenus if not provider_en_pause(m.provider)]
    if sans_pause:
        retenus = sans_pause

    if category in ("classification", "rapide"):
        # Ici on veut le plus RAPIDE, donc le plus petit : tri croissant.
        return sorted(retenus, key=lambda m: (note_de(m) - bonus_fiabilite(m.provider), m.priority))

    if MODEL_SELECTION_MODE == "curated":
        return sorted(retenus, key=lambda m: (m.priority, -note_de(m)))

    return sorted(retenus, key=lambda m: (-(note_de(m) + bonus_fiabilite(m.provider)), m.priority))


def resume() -> dict:
    """Etat de la découverte, pour l'endpoint /models/discover."""
    cache = _charger_cache()
    return {
        "actif": DISCOVERY_ENABLED,
        "mode_selection": MODEL_SELECTION_MODE,
        "derniere_maj": cache.get("maj", 0),
        "age_heures": round((time.time() - cache.get("maj", 0)) / 3600, 1) if cache.get("maj") else None,
        "perime": cache_perime(),
        "nb_modeles_decouverts": len(cache.get("modeles", [])),
        "providers_en_pause": providers_en_pause(),
        "liste_noire": [
            cle for cle, e in cache.get("echecs", {}).items()
            if e.get("n", 0) >= SEUIL_LISTE_NOIRE and time.time() - e.get("t", 0) <= DUREE_LISTE_NOIRE
        ],
        "modeles": sorted(
            cache.get("modeles", []),
            key=lambda m: -m.get("note", 0),
        ),
    }
