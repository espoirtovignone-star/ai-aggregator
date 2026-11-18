"""Small deterministic checks for common mathematical answer errors.

This is a safety net, not a general-purpose computer algebra system.
It mirrors the targeted checks used by AFROACADEMY's existing tutor path.
"""
from __future__ import annotations

import ast
import math
import re
from typing import Any


def _eval_numeric_expression(source: str) -> float | None:
    normalized = source.replace(",", ".")
    normalized = re.sub(r"\\(?:left|right)", "", normalized)
    normalized = re.sub(r"\\(?:cdot|times)", "*", normalized)
    normalized = re.sub(r"\\div", "/", normalized)
    normalized = re.sub(r"\\(?:,|;|!|quad|qquad)", "", normalized)
    for _ in range(5):
        updated = re.sub(r"\\(?:d|t)?frac\{([^{}]+)\}\{([^{}]+)\}", r"(\1)/(\2)", normalized)
        if updated == normalized:
            break
        normalized = updated
    normalized = re.sub(r"\\sqrt\{([^{}]+)\}", r"sqrt(\1)", normalized)
    normalized = normalized.replace("^", "**").strip()
    if not normalized or len(normalized) > 500:
        return None
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError:
        return None

    def evaluate(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Pow):
                if abs(right) > 100:
                    raise ValueError("exponent too large")
                return left ** right
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "sqrt"
            and len(node.args) == 1
            and not node.keywords
        ):
            return math.sqrt(evaluate(node.args[0]))
        raise ValueError("unsupported expression")

    try:
        result = evaluate(tree)
        return result if math.isfinite(result) else None
    except (ArithmeticError, OverflowError, ValueError, TypeError):
        return None


def verify_arithmetic_equalities(answer: str) -> list[str]:
    findings: list[str] = []
    for line_number, raw_line in enumerate(answer.splitlines(), start=1):
        line = re.sub(r"\$\$?", "", raw_line)
        line = re.sub(r"\\\(|\\\)|\\\[|\\\]", "", line)
        line = re.sub(r"\\(?:text|mathrm|mathbf)\{([^}]*)\}", r"\1", line)
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if not line or re.search(r"\\(?:le|ge|neq|approx|equiv)|<=|>=|:=", line):
            continue
        parts = line.split("=")
        if len(parts) < 2 or len(parts) > 4:
            continue
        for index in range(len(parts) - 1):
            left = _eval_numeric_expression(parts[index])
            right = _eval_numeric_expression(parts[index + 1])
            if left is None or right is None:
                continue
            tolerance = 1e-9 * max(1.0, abs(left), abs(right))
            if abs(left - right) > tolerance:
                findings.append(
                    f"Ligne {line_number}: {line} (valeurs calculées : {left:.10g} et {right:.10g})."
                )
                break
    return findings


def verify_symbolic_math(answer: str) -> list[str]:
    normalized = answer.lower()
    normalized = re.sub(r"\\(?:left|right)", "", normalized)
    normalized = re.sub(r"\\(?:,|;|!|quad|qquad)", "", normalized)
    normalized = re.sub(r"[{}$\\]", "", normalized).replace("²", "^2")
    normalized = re.sub(r"\s+", "", normalized)
    target = "3x^2+ax+b" in normalized and "x^2+1" in normalized
    if not target:
        return []
    misplaced_six = (
        "-ax^2-2bx+a+6" in normalized
        or "f'(0)=a+6" in normalized
        or "f′(0)=a+6" in normalized
    )
    if misplaced_six:
        return [
            "Erreur symbolique détectée : pour f(x)=(3x²+ax+b)/(x²+1), le numérateur correct de f'(x) est -ax² + (6 - 2b)x + a. En particulier, f'(0)=a."
        ]
    return []


def verify_asymptote_conclusions(answer: str) -> list[str]:
    normalized = answer.lower()
    normalized = re.sub(r"\\(?:left|right)", "", normalized)
    normalized = re.sub(r"[{}$\\]", "", normalized).replace("²", "^2")
    normalized = re.sub(r"\s+", "", normalized)
    target = "x^2-3x+2" in normalized and "x-1" in normalized and "frac" in normalized
    affine = bool(re.search(r"f\(x\)=x[-+]\d+(?:\.\d+)?", normalized) or re.search(r"f\(x\)=-x[-+]\d+(?:\.\d+)?", normalized))
    findings: list[str] = []
    denies = any(term in normalized for term in ("aucuneasymptote", "pasd'asymptote", "pasd’asymptote", "n'apasd'asymptote", "n’apasd’asymptote"))
    if target and affine and denies:
        findings.append(
            "Conclusion d’asymptote probablement erronée : une expression affine non constante issue de la simplification admet une asymptote oblique à l’infini. Vérifie aussi les éventuelles asymptotes verticales."
        )
    if target and affine:
        global_increase = any(term in normalized for term in ("fonctionesttoujourscroissante", "festsimplementcroissante", "croissantesurtoutsondomaine", "strictementcroissantesurtoutsondomaine"))
        has_table = "tableaudevariations" in normalized or "variations" in normalized
        treats_hole_as_value = bool(re.search(r"f\(1\)=?-?1", normalized) or re.search(r"x[^\n]{0,100}1[^\n]{0,100}f\(x\)[^\n]{0,100}-1", normalized))
        if global_increase or (has_table and treats_hole_as_value):
            findings.append(
                "Erreur de domaine et de variations : pour f(x)=(x²−3x+2)/(x−1), x=1 est exclu. Sépare les intervalles ]−∞;1[ et ]1;+∞[, écris que f(1) n’existe pas et ne relie pas les deux intervalles."
            )
    return findings


def verify_reference_rational_function(answer: str) -> list[str]:
    normalized = answer.lower()
    normalized = re.sub(r"\\(?:d)?frac\{([^{}]+)\}\{([^{}]+)\}", r"(\1)/(\2)", normalized)
    normalized = re.sub(r"\\(?:left|right)", "", normalized)
    normalized = normalized.replace("²", "^2")
    normalized = re.sub(r"[{}$\\]", "", normalized)
    normalized = re.sub(r"\s+", "", normalized)
    if not ("f(x)" in normalized and "x^2+1" in normalized and "x-1" in normalized):
        return []
    findings: list[str] = []
    derivative_present = bool(re.search(r"f['′]\(x\)=", normalized))
    if derivative_present and "x^2-2x-1" not in normalized:
        findings.append(
            "Contrôle de la dérivée : pour f(x)=(x²+1)/(x−1), la dérivée attendue est (x²−2x−1)/(x−1)². Recalcule avec la règle du quotient."
        )
    denies = any(term in normalized for term in ("aucuneasymptote", "pasd'asymptote", "pasd’asymptote"))
    if denies:
        findings.append("Contrôle des asymptotes : cette fonction admet l'asymptote verticale x=1 et l'asymptote oblique y=x+1.")
    return findings


def verify_math_answer(answer: str) -> list[str]:
    return (
        verify_arithmetic_equalities(answer)
        + verify_symbolic_math(answer)
        + verify_asymptote_conclusions(answer)
        + verify_reference_rational_function(answer)
    )
