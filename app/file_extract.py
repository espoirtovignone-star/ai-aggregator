"""
Extraction de texte à partir d'un fichier importé (PDF, Word, texte, etc.)
pour l'injecter comme contexte dans un prompt envoyé à l'IA.

Fonction publique : extract_text(filename, raw_bytes) -> str
Lève UnsupportedFileType si le format n'est pas géré.
"""
import csv
import io
import json
import os

# Extensions traitées comme du texte brut (juste un décodage, pas de
# transformation) — la liste couvre les cas d'usage réels de Mon Bureau.
PLAIN_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".html", ".htm", ".css", ".js",
    ".py", ".yml", ".yaml", ".xml", ".log",
}

SUPPORTED_EXTENSIONS = PLAIN_TEXT_EXTENSIONS | {".pdf", ".docx", ".csv", ".json"}


class UnsupportedFileType(Exception):
    pass


def _decode(raw: bytes) -> str:
    """Décode des octets en texte en essayant plusieurs encodages courants,
    au cas où le fichier ne soit pas en UTF-8 (ex: export Windows)."""
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_pdf(raw: bytes) -> str:
    import pdfplumber

    pages_text = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages_text.append(text.strip())
    return "\n\n".join(pages_text)


def _extract_docx(raw: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(raw))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]

    # Récupère aussi le texte des tableaux (souvent l'essentiel dans les
    # documents administratifs ou financiers).
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))

    return "\n\n".join(parts)


def _extract_csv(raw: bytes) -> str:
    text = _decode(raw)
    reader = csv.reader(io.StringIO(text))
    rows = [" | ".join(cell.strip() for cell in row) for row in reader]
    return "\n".join(rows)


def _extract_json(raw: bytes) -> str:
    text = _decode(raw)
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, ValueError):
        return text


def extract_text(filename: str, raw: bytes) -> str:
    ext = os.path.splitext(filename)[1].lower()

    if ext == ".pdf":
        return _extract_pdf(raw)
    if ext == ".docx":
        return _extract_docx(raw)
    if ext == ".csv":
        return _extract_csv(raw)
    if ext == ".json":
        return _extract_json(raw)
    if ext in PLAIN_TEXT_EXTENSIONS:
        return _decode(raw)

    raise UnsupportedFileType(
        f"Format « {ext or 'inconnu'} » non pris en charge. "
        f"Formats acceptés : PDF, Word (.docx), CSV, JSON, TXT, MD, HTML, CSS, JS, PY."
    )
