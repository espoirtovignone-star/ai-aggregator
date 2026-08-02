"""
Génération de PDF à partir du texte d'une réponse IA.

Le texte reçu est écrit en "markdown léger" (le style que les modèles
utilisent naturellement : # titres, **gras**, listes à tirets, blocs de
code ```). Ce module le transforme en PDF propre et lisible, avec une
page de garde minimale (titre + date) et un pied de page discret.

Fonction publique : build_pdf(content, title) -> bytes
"""
import io
import re
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    ListFlowable,
    ListItem,
    Preformatted,
    PageBreak,
)

ACCENT = HexColor("#4f7cff")
MUTED = HexColor("#6b6f79")
CODE_BG = HexColor("#f4f4f4")

_styles = getSampleStyleSheet()

_title_style = ParagraphStyle(
    "DocTitle", parent=_styles["Title"], textColor=ACCENT, spaceAfter=4,
)
_subtitle_style = ParagraphStyle(
    "DocSubtitle", parent=_styles["Normal"], textColor=MUTED, fontSize=9,
    spaceAfter=18,
)
_h1_style = ParagraphStyle(
    "H1", parent=_styles["Heading1"], textColor=ACCENT, spaceBefore=14, spaceAfter=8,
)
_h2_style = ParagraphStyle(
    "H2", parent=_styles["Heading2"], spaceBefore=10, spaceAfter=6,
)
_body_style = ParagraphStyle(
    "Body", parent=_styles["Normal"], fontSize=10.5, leading=15,
    spaceAfter=8, alignment=TA_LEFT,
)
_bullet_style = ParagraphStyle(
    "Bullet", parent=_body_style, spaceAfter=2,
)
_code_style = ParagraphStyle(
    "Code", parent=_styles["Code"], fontSize=9, leading=12,
    backColor=CODE_BG, borderPadding=8,
)
_footer_style = ParagraphStyle(
    "Footer", parent=_styles["Normal"], fontSize=8, textColor=MUTED,
)


def _inline_markdown_to_reportlab(text: str) -> str:
    """Convertit **gras**, *italique* et `code` en balises reportlab, en
    échappant d'abord le texte brut pour éviter tout conflit avec le XML
    interne de reportlab."""
    text = escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', text)
    return text


def _parse_blocks(content: str):
    """Découpe le texte en blocs (titres, paragraphes, listes, code) en
    suivant une syntaxe markdown minimale — celle que produisent les
    modèles de langage dans 95% des cas."""
    lines = content.replace("\r\n", "\n").split("\n")
    blocks = []
    i = 0
    paragraph_buffer: list[str] = []
    list_buffer: list[str] = []

    def flush_paragraph():
        if paragraph_buffer:
            blocks.append(("p", " ".join(paragraph_buffer).strip()))
            paragraph_buffer.clear()

    def flush_list():
        if list_buffer:
            blocks.append(("ul", list(list_buffer)))
            list_buffer.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_paragraph()
            flush_list()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append(("code", "\n".join(code_lines)))
            i += 1
            continue

        if stripped.startswith("## "):
            flush_paragraph()
            flush_list()
            blocks.append(("h2", stripped[3:].strip()))
            i += 1
            continue

        if stripped.startswith("# "):
            flush_paragraph()
            flush_list()
            blocks.append(("h1", stripped[2:].strip()))
            i += 1
            continue

        if stripped.startswith(("- ", "* ")):
            flush_paragraph()
            list_buffer.append(stripped[2:].strip())
            i += 1
            continue

        if not stripped:
            flush_paragraph()
            flush_list()
            i += 1
            continue

        flush_list()
        paragraph_buffer.append(stripped)
        i += 1

    flush_paragraph()
    flush_list()
    return blocks


def build_pdf(content: str, title: str = "Document") -> bytes:
    """Génère un PDF à partir d'un texte (markdown léger) et retourne les
    octets bruts du fichier, prêts à être renvoyés au navigateur."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=2.2 * cm,
        rightMargin=2.2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=title,
    )

    story = [
        Paragraph(escape(title), _title_style),
        Paragraph(
            f"Généré par l'Agrégateur IA — {datetime.now().strftime('%d/%m/%Y à %H:%M')}",
            _subtitle_style,
        ),
    ]

    for kind, value in _parse_blocks(content):
        if kind == "h1":
            story.append(Paragraph(_inline_markdown_to_reportlab(value), _h1_style))
        elif kind == "h2":
            story.append(Paragraph(_inline_markdown_to_reportlab(value), _h2_style))
        elif kind == "p":
            story.append(Paragraph(_inline_markdown_to_reportlab(value), _body_style))
        elif kind == "ul":
            items = [
                ListItem(Paragraph(_inline_markdown_to_reportlab(v), _bullet_style), leftIndent=6)
                for v in value
            ]
            story.append(ListFlowable(items, bulletType="bullet", start="•", leftIndent=14))
            story.append(Spacer(1, 8))
        elif kind == "code":
            story.append(Preformatted(value, _code_style))
            story.append(Spacer(1, 8))

    if not story[2:]:
        story.append(Paragraph("(contenu vide)", _body_style))

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(2.2 * cm, 1.2 * cm, "Mon Bureau — Agrégateur IA")
        canvas.drawRightString(A4[0] - 2.2 * cm, 1.2 * cm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()
