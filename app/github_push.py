"""
Pousser un fichier généré par l'agrégateur dans un dépôt local géré par
GitHub Desktop : écriture du fichier dans le bon dossier, puis (optionnel)
commit et push automatiques.

Les dépôts sont les clones locaux présents dans GITHUB_REPOS_DIR
(par défaut : le dossier parent de ce projet, là où GitHub Desktop range
tes dépôts). Le front n'a accès qu'à cette liste : on ne peut écrire nulle
part ailleurs sur le disque.

Sécurité :
- Aucune écriture hors du dossier du dépôt choisi (protection anti « ../ »).
- Jamais d'écriture dans .git/.
- Les tokens présents dans les URL de remote (ghp_…, github_pat_…) ne sont
  JAMAIS renvoyés ni loggués : toute sortie git est nettoyée avant de sortir.
- git tourne avec GIT_TERMINAL_PROMPT=0 : en cas de credentials manquants,
  il échoue immédiatement au lieu de bloquer sur une invite.
"""
import os
import re
import subprocess
from dataclasses import dataclass

# Racine du projet ai-aggregator, puis son dossier parent (= là où vivent les
# autres dépôts clonés par GitHub Desktop, typiquement C:\Users\...\Documents).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_REPOS_DIR = os.path.dirname(_PROJECT_ROOT)

REPOS_DIR = os.path.abspath(os.environ.get("GITHUB_REPOS_DIR") or _DEFAULT_REPOS_DIR)

# Délai max (secondes) accordé à un push avant abandon.
PUSH_TIMEOUT = int(os.environ.get("GITHUB_PUSH_TIMEOUT", "90"))

# Dossiers qu'on ne propose pas comme destination (bruit / hors sujet).
_IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                 ".idea", ".vscode", "dist", "build", ".next", ".gradle"}

# Motifs de secrets à effacer de toute sortie renvoyée au client.
_SECRET_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
)
# user:token@host  ->  host  (efface les identifiants embarqués dans une URL)
_URL_CRED_RE = re.compile(r"(https?://)[^/@\s]+@")


class GithubPushError(Exception):
    """Erreur fonctionnelle renvoyée telle quelle à l'utilisateur (déjà nettoyée)."""


def scrub(text: str | None) -> str:
    """Retire tout secret (token, identifiants d'URL) d'une chaîne."""
    if not text:
        return ""
    text = _URL_CRED_RE.sub(r"\1", text)
    text = _SECRET_RE.sub("***", text)
    return text


def _git(repo_root: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )


# ── Découverte des dépôts ─────────────────────────────────────────────────────

@dataclass
class RepoInfo:
    name: str
    path: str
    branch: str
    remote_host: str      # ex: "github.com/user/repo" — sans aucun token
    is_github: bool
    dirty: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "branch": self.branch,
            "remote": self.remote_host,
            "is_github": self.is_github,
            "dirty": self.dirty,
        }


def _remote_host(repo_root: str) -> str:
    proc = _git(repo_root, "remote", "get-url", "origin")
    if proc.returncode != 0:
        return ""
    url = scrub(proc.stdout.strip())
    # Normalise en "github.com/owner/repo" pour l'affichage.
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^git@", "", url).replace(":", "/", 1)
    return url.removesuffix(".git")


def _current_branch(repo_root: str) -> str:
    proc = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _is_dirty(repo_root: str) -> bool:
    proc = _git(repo_root, "status", "--porcelain")
    return bool(proc.stdout.strip())


def list_repos() -> list[RepoInfo]:
    """Tous les dépôts git présents directement sous REPOS_DIR."""
    repos: list[RepoInfo] = []
    try:
        entries = sorted(os.scandir(REPOS_DIR), key=lambda e: e.name.lower())
    except FileNotFoundError:
        return repos

    for entry in entries:
        if not entry.is_dir():
            continue
        if not os.path.isdir(os.path.join(entry.path, ".git")):
            continue
        host = _remote_host(entry.path)
        repos.append(
            RepoInfo(
                name=entry.name,
                path=entry.path,
                branch=_current_branch(entry.path),
                remote_host=host,
                is_github="github.com" in host,
                dirty=_is_dirty(entry.path),
            )
        )
    return repos


def _resolve_repo(name: str) -> str:
    """Retrouve le chemin d'un dépôt par son nom, en validant qu'il est autorisé."""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise GithubPushError("Nom de dépôt invalide.")
    candidate = os.path.abspath(os.path.join(REPOS_DIR, name))
    if os.path.dirname(candidate) != REPOS_DIR:
        raise GithubPushError("Dépôt hors du dossier autorisé.")
    if not os.path.isdir(os.path.join(candidate, ".git")):
        raise GithubPushError(f"« {name} » n'est pas un dépôt git.")
    return candidate


def list_folders(repo_root: str, max_depth: int = 2) -> list[str]:
    """Dossiers existants du dépôt (relatifs), pour suggérer une destination."""
    found: list[str] = []

    def _walk(current: str, rel: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted(os.scandir(current), key=lambda e: e.name.lower())
        except OSError:
            return
        for child in children:
            if not child.is_dir() or child.name in _IGNORED_DIRS or child.name.startswith("."):
                continue
            child_rel = f"{rel}/{child.name}" if rel else child.name
            found.append(child_rel)
            _walk(child.path, child_rel, depth + 1)

    _walk(repo_root, "", 1)
    return found


# ── Écriture + commit + push ──────────────────────────────────────────────────

def _safe_target(repo_root: str, rel_dir: str, filename: str) -> tuple[str, str]:
    """
    Construit le chemin absolu de destination en garantissant qu'il reste à
    l'intérieur du dépôt et jamais dans .git/. Renvoie (chemin_absolu, chemin_relatif).
    """
    filename = os.path.basename((filename or "").strip())
    if not filename or filename in (".", ".."):
        raise GithubPushError("Nom de fichier invalide.")

    rel_dir = (rel_dir or "").strip().strip("/\\").replace("\\", "/")
    rel_parts = [p for p in rel_dir.split("/") if p and p not in (".",)]
    if any(p == ".." for p in rel_parts):
        raise GithubPushError("Chemin de dossier invalide (« .. » interdit).")

    abs_target = os.path.abspath(os.path.join(repo_root, *rel_parts, filename))
    if abs_target != repo_root and not abs_target.startswith(repo_root + os.sep):
        raise GithubPushError("Le chemin sort du dépôt.")

    rel_final = os.path.relpath(abs_target, repo_root).replace("\\", "/")
    if rel_final.split("/")[0] == ".git":
        raise GithubPushError("Écriture dans .git interdite.")
    return abs_target, rel_final


def push_file(
    repo_name: str,
    rel_dir: str,
    filename: str,
    content: str,
    message: str = "",
    do_commit: bool = True,
    do_push: bool = True,
    overwrite: bool = True,
) -> dict:
    """
    Écrit `content` dans <repo>/<rel_dir>/<filename>, puis commit + push
    selon les options. Renvoie un rapport détaillé étape par étape.
    """
    repo_root = _resolve_repo(repo_name)
    abs_target, rel_final = _safe_target(repo_root, rel_dir, filename)

    existed = os.path.exists(abs_target)
    if existed and not overwrite:
        raise GithubPushError(
            f"Le fichier « {rel_final} » existe déjà. Coche « écraser » pour le remplacer."
        )

    os.makedirs(os.path.dirname(abs_target), exist_ok=True)
    with open(abs_target, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    report: dict = {
        "repo": repo_name,
        "path": rel_final,
        "action": "overwritten" if existed else "created",
        "committed": False,
        "pushed": False,
        "branch": _current_branch(repo_root),
        "message": "",
    }

    if not do_commit:
        report["message"] = f"Fichier écrit : {rel_final} (ni commit ni push)."
        return report

    # git add
    add = _git(repo_root, "add", "--", rel_final)
    if add.returncode != 0:
        raise GithubPushError("git add a échoué : " + scrub(add.stderr))

    # Rien à committer (contenu identique à la version déjà suivie) ?
    staged = _git(repo_root, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        report["message"] = (
            f"Fichier écrit : {rel_final}. Aucun changement à committer "
            "(contenu identique à la version déjà versionnée)."
        )
        return report

    commit_msg = (message or "").strip() or f"Ajout de {rel_final} via l'agrégateur IA"
    commit = _git(repo_root, "commit", "-m", commit_msg, "--", rel_final)
    if commit.returncode != 0:
        raise GithubPushError(
            "git commit a échoué : " + scrub(commit.stderr or commit.stdout)
            + " (astuce : vérifie que user.name et user.email sont configurés)"
        )
    report["committed"] = True

    if not do_push:
        report["message"] = f"Commit créé pour {rel_final}. Push à faire dans GitHub Desktop."
        return report

    try:
        push = _git(repo_root, "push", timeout=PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise GithubPushError(
            f"Le push a dépassé {PUSH_TIMEOUT}s et a été abandonné. "
            "Le commit est bien créé en local : tu peux pousser depuis GitHub Desktop."
        )
    if push.returncode != 0:
        raise GithubPushError(
            "Commit créé, mais le push a échoué : " + scrub(push.stderr or push.stdout)
            + " (tu peux pousser depuis GitHub Desktop)."
        )
    report["pushed"] = True
    report["message"] = f"{rel_final} → commit + push OK sur {report['branch']}."
    return report
