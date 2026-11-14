"""
Agrégateur d'IA intelligent — API principale.

Lancer avec :
    uvicorn app.main:app --reload

Endpoints :
    POST /chat            -> {prompt: str}  =>  réponse + métadonnées de routage
                              (provider + model_id optionnels pour forcer un modèle précis)
    POST /prompt/analyze  -> {prompt: str}  =>  thème détecté + prompt optimisé + diff pédagogique
    POST /youtube/fiche   -> {url: str}     =>  fiche de révision à partir d'une vidéo YouTube
    GET  /models          -> liste des modèles gratuits dispo, groupés par catégorie
    GET  /models/discover -> catalogue découvert en direct chez les providers
                              (?refresh=true pour forcer une nouvelle interrogation)
    GET  /github/repos    -> dépôts locaux GitHub Desktop détectés (+ leurs dossiers)
    POST /github/push     -> écrit un fichier dans un dépôt, puis commit + push
    GET  /status          -> état des quotas par provider
"""
import asyncio
import logging
import os
import re
import unicodedata

from fastapi import Depends, FastAPI, HTTPException, Header, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.router import classify_prompt, pick_and_call, call_premium, NoModelAvailable, BudgetExceeded, InvalidModel
from app.team import run_team
from app.quota import status as quota_status
from app.budget import status as budget_status
from app.config import MODELS, CATEGORIES, PROVIDERS, APP_ACCESS_TOKEN
from app.youtube import extract_video_id, fetch_video_title, fetch_transcript, YoutubeError
from app.prompts import build_revision_prompt
from app.prompt_analyzer import analyze_prompt
from app.pdf_export import build_pdf
from app.file_extract import extract_text, UnsupportedFileType
from app.model_discovery import candidats, rafraichir, resume as discovery_resume
from app.config import DISCOVERY_ENABLED, MODEL_SELECTION_MODE
from app.github_push import list_repos, list_folders, push_file, GithubPushError

logger = logging.getLogger(__name__)

app = FastAPI(title="Agrégateur IA", version="0.5.0")


@app.on_event("startup")
async def _decouverte_au_demarrage():
    """
    Au démarrage, on va voir en arrière-plan si de nouveaux modèles gratuits
    sont apparus chez les providers. En arrière-plan = le serveur répond tout de
    suite, la découverte se termine toute seule quelques secondes après.
    """
    if not DISCOVERY_ENABLED:
        return

    async def _tache():
        try:
            res = await rafraichir()
            logger.info("[Découverte] %s modèles gratuits connus", res.get("modeles"))
        except Exception as e:  # jamais bloquant
            logger.warning("[Découverte] échec au démarrage : %s", e)

    asyncio.create_task(_tache())

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def require_token(x_access_token: str | None = Header(None)) -> None:
    """Si APP_ACCESS_TOKEN est défini (usage exposé hors machine locale), vérifie le header."""
    if APP_ACCESS_TOKEN and x_access_token != APP_ACCESS_TOKEN:
        raise HTTPException(401, "Code d'accès invalide ou manquant.")


class ChatRequest(BaseModel):
    prompt: str
    category: str | None = None
    provider: str | None = None
    model_id: str | None = None
    premium: bool = False
    premium_provider: str = "anthropic"
    # qualite=True : pour CET appel seulement, on vise le modèle le mieux noté
    # même s'il est chez un provider au quota avare (OpenRouter). À réserver
    # aux analyses importantes — c'est ce qui consomme le quota gratuit.
    qualite: bool = False


class ChatResponse(BaseModel):
    response: str
    category: str
    provider: str
    model: str
    cost_usd: float | None = None


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_token)])
async def chat(req: ChatRequest):
    if not req.prompt.strip():
        raise HTTPException(400, "Le prompt est vide.")

    if req.premium:
        if req.premium_provider not in ("anthropic", "openai", "nvidia"):
            raise HTTPException(400, "premium_provider doit être 'anthropic', 'openai' ou 'nvidia'.")
        if req.premium_provider == "nvidia":
            model_id = os.environ.get("NVIDIA_MODEL_ID", "").strip()
            if not model_id:
                raise HTTPException(400, "NVIDIA_MODEL_ID n'est pas configuré dans Render.")
        else:
            model_id = "claude-sonnet-4-6" if req.premium_provider == "anthropic" else "gpt-5-mini"
        try:
            content, usage = await call_premium(req.prompt, req.premium_provider, model_id)
        except BudgetExceeded as e:
            raise HTTPException(402, str(e))
        return ChatResponse(
            response=content,
            category="premium",
            provider=req.premium_provider,
            model=model_id,
            cost_usd=usage["cost_usd"],
        )

    category = req.category or await classify_prompt(req.prompt)

    try:
        provider, model_id, result = await pick_and_call(
            req.prompt,
            category,
            forced_provider=req.provider,
            forced_model_id=req.model_id,
            mode="qualite" if req.qualite else None,
        )
    except InvalidModel as e:
        raise HTTPException(400, str(e))
    except NoModelAvailable as e:
        raise HTTPException(503, str(e))

    return ChatResponse(response=result, category=category, provider=provider, model=model_id)


# ── /prompt/analyze ───────────────────────────────────────────────────────────

class PromptAnalyzeRequest(BaseModel):
    prompt: str


class PromptAnalyzeResponse(BaseModel):
    theme: str
    theme_label: str
    confidence: float
    source_tier: str
    original: str
    optimized: str
    changes: list[str]
    explanation: str


@app.post("/prompt/analyze", response_model=PromptAnalyzeResponse, dependencies=[Depends(require_token)])
async def prompt_analyze(req: PromptAnalyzeRequest):
    """
    Détecte le thème d'un prompt et propose une version optimisée avec
    la liste des changements — affichée côté front avant envoi pour que
    l'utilisateur apprenne à mieux formuler ses prompts.
    """
    if not req.prompt.strip():
        raise HTTPException(400, "Le prompt est vide.")
    result = await analyze_prompt(req.prompt.strip())
    return PromptAnalyzeResponse(**result)


# ── /youtube/fiche ────────────────────────────────────────────────────────────

class YoutubeFicheRequest(BaseModel):
    url: str
    provider: str | None = None
    model_id: str | None = None


class YoutubeFicheResponse(BaseModel):
    title: str
    video_id: str
    fiche: str
    category: str
    provider: str
    model: str


@app.post("/youtube/fiche", response_model=YoutubeFicheResponse, dependencies=[Depends(require_token)])
async def youtube_fiche(req: YoutubeFicheRequest):
    try:
        video_id = extract_video_id(req.url)
    except YoutubeError as e:
        raise HTTPException(400, str(e))

    title = await fetch_video_title(video_id)

    try:
        transcript = fetch_transcript(video_id)
    except YoutubeError as e:
        raise HTTPException(422, str(e))

    if not transcript.strip():
        raise HTTPException(422, "Transcript vide — rien à résumer.")

    prompt = build_revision_prompt(title, transcript, req.url)

    try:
        provider, model_id, result = await pick_and_call(
            prompt,
            "contexte_long",
            forced_provider=req.provider,
            forced_model_id=req.model_id,
        )
    except InvalidModel as e:
        raise HTTPException(400, str(e))
    except NoModelAvailable as e:
        raise HTTPException(503, str(e))

    return YoutubeFicheResponse(
        title=title,
        video_id=video_id,
        fiche=result,
        category="contexte_long",
        provider=provider,
        model=model_id,
    )


# ── /models ───────────────────────────────────────────────────────────────────

@app.get("/models", dependencies=[Depends(require_token)])
async def list_models():
    """
    Liste des modèles gratuits. "curated" = écrits à la main dans config.py.
    "ordre_effectif" = l'ordre dans lequel l'agrégateur les essaie VRAIMENT
    pour chaque catégorie, découverte automatique comprise.
    """
    return {
        "categories": CATEGORIES,
        "models": [
            {
                "provider": m.provider,
                "model_id": m.model_id,
                "categories": m.categories,
                "priority": m.priority,
                "source": "curated",
            }
            for m in MODELS
            if PROVIDERS[m.provider].is_free
        ],
        "mode_selection": MODEL_SELECTION_MODE,
        "ordre_effectif": {
            cat: [f"{m.provider}/{m.model_id}" for m in candidats(cat)[:8]]
            for cat in CATEGORIES
        },
        "ordre_si_qualite": {
            cat: [f"{m.provider}/{m.model_id}" for m in candidats(cat, "qualite")[:5]]
            for cat in CATEGORIES
        },
    }


@app.get("/models/discover", dependencies=[Depends(require_token)])
async def models_discover(refresh: bool = False):
    """
    Ce que les providers proposent gratuitement EN CE MOMENT.
    ?refresh=true force une nouvelle interrogation (sinon, cache de 24 h).
    """
    if refresh or not DISCOVERY_ENABLED:
        await rafraichir(force=refresh)
    else:
        await rafraichir(force=False)
    return discovery_resume()


# ── /team ─────────────────────────────────────────────────────────────────────

class TeamStep(BaseModel):
    step: str
    provider: str
    model: str


class TeamResponse(BaseModel):
    response: str
    plan: str
    draft: str
    steps: list[TeamStep]
    category: str
    cost_usd: float


@app.post("/team", response_model=TeamResponse, dependencies=[Depends(require_token)])
async def team(req: ChatRequest):
    if not req.prompt.strip():
        raise HTTPException(400, "Le prompt est vide.")
    try:
        result = await run_team(req.prompt)
    except NoModelAvailable as e:
        raise HTTPException(503, str(e))
    except BudgetExceeded as e:
        raise HTTPException(402, str(e))
    return TeamResponse(**result)


# ── /export/pdf ───────────────────────────────────────────────────────────────
# Les autres formats (md, html, json, css) sont générés directement dans le
# navigateur (aucun besoin de rendu serveur) — voir app/static/index.html.
# Seul le PDF a besoin d'une vraie mise en page, donc un aller-retour serveur.

def _safe_filename(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return slug[:60] or "document"


class ExportPdfRequest(BaseModel):
    content: str
    title: str | None = None


@app.post("/export/pdf", dependencies=[Depends(require_token)])
async def export_pdf(req: ExportPdfRequest):
    if not req.content.strip():
        raise HTTPException(400, "Rien à exporter : le contenu est vide.")

    title = (req.title or "Document").strip() or "Document"
    pdf_bytes = build_pdf(req.content, title)
    filename = f"{_safe_filename(title)}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── /import/extract ──────────────────────────────────────────────────────────
# Extrait le texte d'un fichier importé (PDF, Word, CSV, JSON, texte) pour
# qu'il soit joint automatiquement au prochain message envoyé à l'IA.

MAX_IMPORT_SIZE_BYTES = 15 * 1024 * 1024  # 15 Mo
MAX_IMPORT_CHARS = 20_000  # ~5000 tokens : au-delà, ça mange le quota gratuit pour rien


class ImportExtractResponse(BaseModel):
    filename: str
    content: str
    char_count: int
    truncated: bool


@app.post("/import/extract", response_model=ImportExtractResponse, dependencies=[Depends(require_token)])
async def import_extract(file: UploadFile = File(...)):
    raw = await file.read()

    if len(raw) > MAX_IMPORT_SIZE_BYTES:
        raise HTTPException(413, "Fichier trop volumineux (limite : 15 Mo).")

    filename = file.filename or "fichier"

    try:
        text = extract_text(filename, raw)
    except UnsupportedFileType as e:
        raise HTTPException(415, str(e))
    except Exception as e:
        raise HTTPException(422, f"Impossible de lire ce fichier : {e}")

    text = text.strip()
    if not text:
        raise HTTPException(422, "Aucun texte n'a pu être extrait de ce fichier (image scannée sans OCR, page vide…).")

    truncated = len(text) > MAX_IMPORT_CHARS
    if truncated:
        text = text[:MAX_IMPORT_CHARS]

    return ImportExtractResponse(
        filename=filename,
        content=text,
        char_count=len(text),
        truncated=truncated,
    )


# ── /github ───────────────────────────────────────────────────────────────────
# Pousser un fichier généré dans un dépôt local géré par GitHub Desktop :
# écriture dans le bon dossier, puis commit + push automatiques.


@app.get("/github/repos", dependencies=[Depends(require_token)])
async def github_repos():
    """Liste les dépôts locaux détectés + leurs dossiers, pour peupler l'UI."""
    repos = list_repos()
    return {
        "repos": [
            {**r.to_dict(), "folders": list_folders(r.path)}
            for r in repos
        ]
    }


class GithubPushRequest(BaseModel):
    repo: str
    path: str = ""          # sous-dossier de destination, ex: "src/pages"
    filename: str
    content: str
    message: str | None = None
    commit: bool = True
    push: bool = True
    overwrite: bool = True


@app.post("/github/push", dependencies=[Depends(require_token)])
async def github_push(req: GithubPushRequest):
    if not req.content.strip():
        raise HTTPException(400, "Rien à pousser : le contenu est vide.")
    if not req.filename.strip():
        raise HTTPException(400, "Nom de fichier manquant.")

    # git peut bloquer quelques secondes (push réseau) : on ne bloque pas la boucle asyncio.
    try:
        report = await asyncio.to_thread(
            push_file,
            req.repo,
            req.path,
            req.filename,
            req.content,
            req.message or "",
            req.commit,
            req.push and req.commit,  # pas de push sans commit
            req.overwrite,
        )
    except GithubPushError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # garde-fou : ne jamais laisser fuiter un chemin/token brut
        from app.github_push import scrub
        raise HTTPException(500, "Échec inattendu : " + scrub(str(e)))

    return report


# ── /status & / ──────────────────────────────────────────────────────────────

@app.get("/status", dependencies=[Depends(require_token)])
async def status():
    return {"quota": quota_status(), "budget": budget_status()}


@app.get("/")
async def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
