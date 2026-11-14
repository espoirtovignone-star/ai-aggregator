"""
Configuration centrale de l'agrégateur.
Toutes les clés API viennent des variables d'environnement (voir .env.example).
"""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str
    rpm: int
    rpd: int
    tpm: int | None = None  # tokens/minute — None si non publié par le provider
    is_free: bool = True
    # prix par million de tokens (input, output) — uniquement pour les providers payants
    price_per_mtok: tuple[float, float] | None = None


PROVIDERS: dict[str, ProviderConfig] = {
    "groq": ProviderConfig(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="GROQ_API_KEY",
        rpm=30,
        rpd=1000,
        tpm=6_000,
    ),
    "gemini": ProviderConfig(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
        rpm=10,
        rpd=1500,
        tpm=250_000,
    ),
    "openrouter": ProviderConfig(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        rpm=20,
        rpd=50,  # passe à 1000 si 10$ de crédits achetés une fois
    ),
    "cloudflare": ProviderConfig(
        name="cloudflare",
        # L'URL complète inclut l'Account ID, ajouté par providers/client.py.
        base_url="https://api.cloudflare.com/client/v4/accounts",
        api_key_env="CLOUDFLARE_API_TOKEN",
        rpm=5,
        rpd=100,
        tpm=None,
        # Workers AI est facturé au-delà de l’allocation gratuite du compte.
        # Activation automatique uniquement après validation explicite du propriétaire.
        is_free=os.environ.get("CLOUDFLARE_ENABLE_AUTO", "0").lower() in ("1", "true", "yes"),
    ),
    "sambanova": ProviderConfig(
        name="sambanova",
        base_url="https://api.sambanova.ai/v1",
        api_key_env="SAMBANOVA_API_KEY",
        rpm=5,
        rpd=100,
        tpm=None,
    ),
    # --- Providers payants, jamais choisis automatiquement (voir router.py) ---
    "anthropic": ProviderConfig(
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        rpm=50,       # dépend de ton tier réel, ajuste si besoin
        rpd=10_000,   # pas de vraie limite RPD, seul le budget $ compte ici
        is_free=False,
        price_per_mtok=(3.00, 15.00),  # Sonnet 4.6 par défaut
    ),
    "openai": ProviderConfig(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        rpm=50,
        rpd=10_000,
        is_free=False,
        price_per_mtok=(2.50, 10.00),  # à ajuster selon le modèle exact utilisé
    ),
}


@dataclass
class ModelConfig:
    provider: str
    model_id: str
    categories: list[str] = field(default_factory=list)  # ce pour quoi il est bon
    priority: int = 1  # 1 = essayer en premier dans sa catégorie


# Table des modèles disponibles, avec leurs forces.
# L'ordre à l'intérieur d'une catégorie = ordre de préférence (priority croissante).
# Chaque catégorie a plusieurs candidats répartis sur 3 providers différents :
# si un backend précis est congestionné (ex: OpenRouter/Venice), la cascade
# retombe sur un autre provider plutôt que d'échouer entièrement.
MODELS: list[ModelConfig] = [
    # Rapide / classification / petites tâches
    ModelConfig("groq", "llama-3.1-8b-instant", ["classification", "rapide"], priority=1),

    # Code
    ModelConfig("groq", "qwen/qwen3-32b", ["code"], priority=1),
    ModelConfig("openrouter", "qwen/qwen3-coder:free", ["code"], priority=2),
    ModelConfig("groq", "llama-3.3-70b-versatile", ["code"], priority=3),
    ModelConfig("openrouter", "openai/gpt-oss-120b:free", ["code"], priority=4),
    ModelConfig("openrouter", "cohere/north-mini-code:free", ["code"], priority=5),

    # Raisonnement / analyse complexe
    ModelConfig("groq", "openai/gpt-oss-120b", ["raisonnement"], priority=1),
    # groq/compound : ce n'est pas un modèle mais un SYSTEME (modèle + recherche
    # web + exécution de code intégrées). Très utile pour tout ce qui demande de
    # vérifier une info d'actualité (fact-check, analyse d'article de veille).
    ModelConfig("groq", "groq/compound", ["raisonnement"], priority=2),
    ModelConfig("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free", ["raisonnement"], priority=2),
    ModelConfig("groq", "llama-3.3-70b-versatile", ["raisonnement"], priority=3),
    ModelConfig("openrouter", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", ["raisonnement"], priority=4),
    ModelConfig("openrouter", "nvidia/nemotron-3-super-120b-a12b:free", ["raisonnement"], priority=5),

    # Rédaction / créatif
    ModelConfig("groq", "llama-3.3-70b-versatile", ["redaction", "creatif"], priority=1),
    ModelConfig("openrouter", "nousresearch/hermes-3-llama-3.1-405b:free", ["redaction", "creatif"], priority=2),
    ModelConfig("openrouter", "google/gemma-4-31b-it:free", ["redaction", "creatif"], priority=3),
    ModelConfig("openrouter", "meta-llama/llama-3.3-70b-instruct:free", ["redaction", "creatif"], priority=4),

    # Contexte long / documents volumineux
    ModelConfig("gemini", "gemini-2.5-flash", ["contexte_long"], priority=1),
    ModelConfig("gemini", "gemini-2.5-flash-lite", ["contexte_long"], priority=2),
    ModelConfig("groq", "meta-llama/llama-4-scout-17b-16e-instruct", ["contexte_long"], priority=3),
    ModelConfig("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free", ["contexte_long"], priority=4),

    # Généraliste (fallback ultime si tout le reste est à sec)
    # Groq en premier : c'est le provider le plus fiable/rapide en pratique,
    # les deux autres subissent régulièrement de la congestion upstream.
    ModelConfig("groq", "llama-3.3-70b-versatile", ["general"], priority=1),
    ModelConfig("groq", "openai/gpt-oss-20b", ["general"], priority=2),
    ModelConfig("openrouter", "meta-llama/llama-3.3-70b-instruct:free", ["general"], priority=3),
    ModelConfig("openrouter", "nvidia/nemotron-3-nano-30b-a3b:free", ["general"], priority=4),
    ModelConfig("gemini", "gemini-2.5-flash-lite", ["general"], priority=5),
    ModelConfig("openrouter", "openai/gpt-oss-20b:free", ["general"], priority=6),

    # Cloudflare Workers AI et SambaNova : quotas locaux prudents (pas des plafonds
    # contractuels fournisseur). Les modèles sont épinglés pour éviter une découverte
    # de catalogue non vérifiée.
    ModelConfig("cloudflare", "@cf/meta/llama-3.1-8b-instruct-fp8-fast", ["classification", "rapide", "general"], priority=3),
    ModelConfig("sambanova", "Meta-Llama-3.3-70B-Instruct", ["raisonnement", "redaction", "creatif", "general"], priority=3),
    ModelConfig("sambanova", "Qwen3-32B", ["code", "raisonnement", "general"], priority=3),

    # --- Premium (payant) : jamais sélectionnés par le classifieur automatique.
    # Utilisables uniquement via un appel explicite (voir router.call_premium).
    ModelConfig("anthropic", "claude-sonnet-4-6", ["premium"], priority=1),
    ModelConfig("openai", "gpt-5-mini", ["premium"], priority=2),
]

CATEGORIES = ["classification", "code", "raisonnement", "redaction", "creatif", "contexte_long", "general"]
# catégorie séparée : jamais retournée par classify_local, accessible seulement à la demande
PREMIUM_CATEGORY = "premium"

# Plafond de dépense mensuel en dollars pour les providers payants.
# Modifiable via la variable d'env MAX_MONTHLY_SPEND_USD dans .env
# NB: `or "5.0"` et pas un défaut d'argument : une variable présente mais VIDE
# dans .env (MAX_MONTHLY_SPEND_USD=) renvoie "" et ferait planter float("").
MAX_MONTHLY_SPEND_USD = float(os.environ.get("MAX_MONTHLY_SPEND_USD") or "5.0")

# Code d'accès partagé, requis dès que le serveur est exposé hors de ta machine
# (ex: via un tunnel). Si non défini, aucune protection n'est appliquée (usage local uniquement).
APP_ACCESS_TOKEN = os.environ.get("APP_ACCESS_TOKEN")


# ─────────────────────────────────────────────────────────────────────────────
# DECOUVERTE AUTOMATIQUE DES MODELES (voir app/model_discovery.py)
# ─────────────────────────────────────────────────────────────────────────────
# La liste MODELS ci-dessus est la liste "de confiance", écrite à la main.
# En plus de ça, l'agrégateur interroge tout seul les providers pour repérer les
# NOUVEAUX modèles gratuits et les ajouter à la rotation, sans toucher au code.

# Mettre MODEL_DISCOVERY=0 dans les variables d'env pour désactiver complètement.
DISCOVERY_ENABLED = os.environ.get("MODEL_DISCOVERY", "1").lower() not in ("0", "false", "non")

# Au bout de combien d'heures on retourne demander aux providers ce qui est neuf.
DISCOVERY_MAX_AGE_HOURS = float(os.environ.get("MODEL_DISCOVERY_MAX_AGE_HOURS") or "24")

# Comment l'agrégateur choisit quel modèle appeler en premier :
#
# "fiable"  = (DÉFAUT) on privilégie les providers au quota gratuit généreux,
#             et à l'intérieur de chacun on prend le mieux noté. Concrètement :
#             le meilleur modèle Groq d'abord, puis Gemini, puis OpenRouter en
#             dernier recours. Groq autorise des milliers d'appels gratuits par
#             jour ; OpenRouter en autorise une cinquantaine et renvoie des 429
#             dès qu'on insiste. Ce mode évite de brûler le quota OpenRouter
#             sur des tâches courantes (résumés, veille, fact-check).
#
# "qualite" = on appelle TOUJOURS le modèle le mieux noté toutes provenances
#             confondues, même s'il est chez un provider avare. Meilleures
#             réponses, mais quota gratuit épuisé beaucoup plus vite.
#             Utilisable au coup par coup sans changer ce réglage : il suffit
#             d'envoyer {"qualite": true} dans le corps de POST /chat.
#
# "curated" = on respecte l'ordre écrit à la main dans MODELS ci-dessus, les
#             modèles découverts ne servent que de renfort.
MODEL_SELECTION_MODE = os.environ.get("MODEL_SELECTION_MODE", "fiable")

# Ordre de préférence des providers en mode "fiable" (0 = essayé en premier).
# Modifiable sans toucher au code via PROVIDER_ORDER="gemini,groq,openrouter".
_ordre_env = os.environ.get("PROVIDER_ORDER", "groq,gemini,sambanova,cloudflare,openrouter")
PROVIDER_ORDER = [p.strip() for p in _ordre_env.split(",") if p.strip()]

# Fichier où sont stockés les modèles découverts + les modèles en panne.
DISCOVERY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "models_discovered.json")


def get_api_key(provider: str) -> str:
    cfg = PROVIDERS[provider]
    key = os.environ.get(cfg.api_key_env)
    if not key:
        raise RuntimeError(
            f"Clé API manquante pour {provider}. Définis {cfg.api_key_env} dans ton .env"
        )
    return key


QUOTA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "quota.json")
