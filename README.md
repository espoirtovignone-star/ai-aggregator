# Agrégateur IA

Route chaque prompt vers un modèle gratuit activé (Groq, Gemini, OpenRouter), selon la catégorie détectée et le quota restant. Cloudflare Workers AI et SambaNova ont leurs endpoints configurés, mais sont exclus du routage automatique car leur usage peut être facturé.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# remplis les clés des providers que tu souhaites activer
```

Clés / comptes fournisseurs :
- Groq : console.groq.com
- Gemini : aistudio.google.com/apikey
- OpenRouter : openrouter.ai/keys
- Cloudflare Workers AI : dash.cloudflare.com (API Token + Account ID)
- SambaNova : cloud.sambanova.ai (API Key)

Cloudflare facture à l'usage au-delà de l'allocation gratuite du compte. SambaNova Free demande l'achat de crédits avant les requêtes. Les deux providers sont donc exclus de la cascade automatique pour éviter toute dépense non autorisée. Les limites locales ne constituent pas des quotas gratuits garantis.
NVIDIA est préparé séparément et n'est pas routé automatiquement.

## Lancer

```bash
uvicorn app.main:app --reload
```

Puis ouvre `http://127.0.0.1:8000` : interface de chat directement dans le navigateur.

## Accès depuis un autre appareil (téléphone, etc.)

Sur Windows, double-clique `start.bat` : ça lance le serveur **et** un tunnel public
(Cloudflare Tunnel, gratuit, sans compte) dans deux fenêtres séparées. L'URL publique
en `https://....trycloudflare.com` s'affiche dans la fenêtre "Tunnel public" — elle
change à chaque lancement.

Nécessite `tools/cloudflared.exe` (binaire autonome, pas d'installeur) :
https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe

Dès que le serveur est exposé ainsi, définis `APP_ACCESS_TOKEN` dans `.env` (un secret
au choix) : sans ça, n'importe qui avec le lien peut l'utiliser. Le frontend demande
ce code une seule fois et le retient sur l'appareil.

## Déploiement permanent (Render, gratuit)

Pour qu'un autre service (ex: un backend qui appelle cette API en continu) puisse s'y
fier sans dépendre de ton PC allumé + tunnel :

1. Sur [render.com](https://render.com) → **New +** → **Web Service** → connecte le repo GitHub `ai-aggregator`
2. Render détecte Python automatiquement. Configure :
   - **Build Command** : `pip install -r requirements.txt`
   - **Start Command** : `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Dans **Environment**, ajoute les mêmes variables que ton `.env` local :
   `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `APP_ACCESS_TOKEN`
   (et `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` si tu utilises le mode premium)
4. Render te donne une URL fixe du type `https://ai-aggregator-xxxx.onrender.com`

**Limites du plan gratuit Render à connaître** :
- Le service "s'endort" après 15 min sans requête, la première requête suivante prend
  ~30-50s pour le réveiller (normal, pas un bug)
- Le disque n'est **pas persistant** entre redémarrages : `data/quota.json` repart à zéro
  de temps en temps. Sans conséquence grave (juste moins précis sur le tracking de quota
  entre deux redémarrages), pas un problème fonctionnel.

## Mode équipe (plan → exécution → relecture)

Trois modèles collaborent sur la même demande, pour un résultat nettement
meilleur sur les tâches complexes (code, documents, analyses) :

1. **Plan** — Claude structure la demande en plan d'action
2. **Exécution** — le meilleur modèle gratuit de la catégorie réalise la tâche
3. **Relecture** — Claude corrige le brouillon et livre la version finale

Dans l'interface web : coche « Mode équipe ». En API : `POST /team` avec le même
corps que `/chat`.

Pour les étapes Claude, l'agrégateur choisit automatiquement le moyen le moins
cher disponible :

- **CLI Claude Code** (`claude -p`) s'il est installé et connecté sur la machine —
  couvert par l'abonnement Claude Pro/Max, coût zéro. C'est le mode normal en local.
- **API Anthropic** si `ANTHROPIC_API_KEY` est définie (plafond mensuel respecté) —
  seul mode possible sur Render, où le CLI n'existe pas.
- **Meilleur modèle gratuit de raisonnement** en dernier recours (qualité moindre
  mais toujours fonctionnel).

Compter 1 à 3 minutes par requête (3 appels en série). Variable d'env optionnelle :
`CLAUDE_CODE_TIMEOUT` (secondes, défaut 240).

## Pousser une réponse dans un dépôt GitHub Desktop

Sous chaque réponse, le bouton **📤 GitHub** ouvre une fenêtre pour écrire ce
contenu comme fichier dans un de tes dépôts locaux (ceux clonés par GitHub
Desktop), puis **commit + push** automatiquement.

- **Dépôt** : liste déroulante des dépôts git détectés à côté de ce projet.
- **Dossier** : le sous-dossier de destination (autocomplétion des dossiers
  existants du dépôt), vide = racine.
- **Nom de fichier** : prérempli avec une extension devinée d'après le contenu.
- Cases **Créer un commit** / **Pousser (push)** / **Écraser** pour ajuster.

Où l'agrégateur cherche les dépôts : le dossier **parent** de ce projet par
défaut (là où GitHub Desktop range tes clones). Change-le avec la variable
d'env `GITHUB_REPOS_DIR`. Le push tourne avec `GIT_TERMINAL_PROMPT=0` : s'il
manque des identifiants, il échoue tout de suite (pas de blocage), et le commit
local reste fait — tu peux alors pousser depuis GitHub Desktop.

Endpoints correspondants :

```bash
curl localhost:8000/github/repos            # dépôts + dossiers détectés
curl -X POST localhost:8000/github/push -H "Content-Type: application/json" \
  -d '{"repo":"mon-bureau","path":"docs","filename":"notes.md","content":"# Notes","message":"Ajout notes"}'
```

Sécurité : écriture impossible hors du dépôt choisi (protection anti-`..`) et
jamais dans `.git/`. Les tokens présents dans les URL de remote ne sont jamais
renvoyés ni loggués.

## Utiliser (API directe)

```bash
curl -X POST localhost:8000/chat -H "Content-Type: application/json" \
  -d '{"prompt": "Écris une fonction Python pour trier une liste"}'

curl localhost:8000/status
```

## Structure

- `app/config.py`   : providers, modèles, table de routage par catégorie
- `app/quota.py`    : tracker de quota (RPM/RPD) persisté en JSON
- `app/router.py`   : classification du prompt + sélection du modèle avec fallback
- `app/providers/client.py` : client unifié (tous les providers sont OpenAI-compatible)
- `app/main.py`     : API FastAPI (POST /chat, GET /status)

## Prochaines étapes possibles

- Cache des réponses (prompts identiques)
- Dashboard web simple pour visualiser les quotas en temps réel
- Fallback browser automation (Playwright) pour les modèles sans API gratuite
