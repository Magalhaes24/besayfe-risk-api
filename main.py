"""
CLI entrypoint to compute allergen risk for a product EAN.

Flow:
- Parse user inputs (EAN, allergen list, risk flags, output format, data sources).
- Resolve allergen codes to canonical forms.
- Load optional local FoodDB for ingredient enrichment and choose the product
  source (OpenFoodFacts or database).
- Build the RiskEngine and calculate per-allergen scores for the requested
  product.
- Render either a text dashboard or JSON payload and append a history record.
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, Tuple

from risk_engine import (
    DatabaseProductSource,
    FacilityAllergenProfile,
    FoodDatabase,
    OpenFoodFactsClient,
    RiskEngine,
    UserAllergyProfile,
)
from risk_engine.allergens import allergen_label, resolve_allergen_code

# Simple i18n table for CLI output (extendable with more locales).
TRANSLATIONS = {
    "en": {
        "cli_title": "=== Allergen Risk Checker ===",
        "prompt_language": "Preferred language (e.g. en, pt) [en]: ",
        "prompt_ean": "Enter product EAN/barcode: ",
        "select_allergens": "Select allergens (comma-separated numbers):",
        "selection_prompt": "Your selection: ",
        "select_error_numbers": "Use numbers from the list (e.g. 1,3,5).",
        "select_error_empty": "Please select at least one allergen.",
        "select_error_range": "Choices out of range: {invalid}. Try again.",
        "prompt_may_contain": "Consider 'may contain' as risky?",
        "prompt_facility": "Consider facility cross-contact as risky?",
        "section_ingredients": "Ingredients",
        "section_contains": "Declared allergens",
        "section_may_contain": "Traces / may contain",
        "section_facility": "Facility cross-contact",
        "quick_view": "=== Quick view ===",
        "details": "=== Details ===",
        "per_allergen_breakdown": "Per-allergen breakdown:",
        "total_risk": "Total risk",
        "highest_concern": "Highest concern",
        "product_not_found": "Product not found.",
        "risk_very_high": "very high",
        "risk_high": "high",
        "risk_moderate": "moderate",
        "risk_low": "low",
        "risk_very_low": "very low",
        "presence_contains": "contains",
        "presence_may_contain": "may contain",
        "presence_facility_risk": "facility risk",
    },
    "pt": {
        "cli_title": "=== Verificador de Risco de Alérgenos ===",
        "prompt_language": "Idioma preferido (ex.: en, pt) [en]: ",
        "prompt_ean": "Introduza o EAN/código de barras do produto: ",
        "select_allergens": "Selecione os alérgenos (números separados por vírgula):",
        "selection_prompt": "A sua escolha: ",
        "select_error_numbers": "Use os números da lista (ex.: 1,3,5).",
        "select_error_empty": "Escolha pelo menos um alérgeno.",
        "select_error_range": "Opções fora do intervalo: {invalid}. Tente novamente.",
        "prompt_may_contain": "Considerar 'pode conter' como risco?",
        "prompt_facility": "Considerar risco de contaminação cruzada na fábrica?",
        "section_ingredients": "Ingredientes",
        "section_contains": "Alérgenos declarados",
        "section_may_contain": "Traços / pode conter",
        "section_facility": "Risco de fábrica",
        "quick_view": "=== Visão rápida ===",
        "details": "=== Detalhes ===",
        "per_allergen_breakdown": "Análise por alérgeno:",
        "total_risk": "Risco total",
        "highest_concern": "Maior preocupação",
        "product_not_found": "Produto não encontrado.",
        "risk_very_high": "muito alto",
        "risk_high": "alto",
        "risk_moderate": "moderado",
        "risk_low": "baixo",
        "risk_very_low": "muito baixo",
        "presence_contains": "contém",
        "presence_may_contain": "pode conter",
        "presence_facility_risk": "risco na fábrica",
    },
}



def _t(key: str, lang: str = "en") -> str:
    """Translate a key to the requested language with English fallback."""
    bundle = TRANSLATIONS.get(lang, TRANSLATIONS["en"])
    template = bundle.get(key) or TRANSLATIONS["en"].get(key, key)
    return template


def parse_args() -> argparse.Namespace:
    """Configure and parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Calculate allergen risk for a product EAN"
    )
    parser.add_argument("--ean", required=True, help="Product barcode/EAN")
    parser.add_argument(
        "--allergies",
        required=True,
        help="Comma-separated allergen codes (e.g. MILK,GLUTEN,PEANUT)",
    )
    parser.add_argument(
        "--avoid-traces",
        action="store_true",
        default=False,
        help="Consider may_contain warnings as risky",
    )
    parser.add_argument(
        "--avoid-facility-risk",
        action="store_true",
        default=False,
        help="Treat facility-level cross-contact as risky",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--db-dsn",
        default=None,
        help="PostgreSQL DSN (e.g. postgres://user:pass@host:5432/dbname). If set, data is read from DB instead of OpenFoodFacts.",
    )
    parser.add_argument(
        "--food-db",
        dest="food_db_path",
        default=None,
        help="Path to Food.csv from FoodDB for richer ingredient context. Defaults to bundled db/foodb_2020_04_07_csv/Food.csv if present.",
    )
    parser.add_argument(
        "--lang",
        default="en",
        help="Language for output labels (e.g. en, pt). Defaults to en.",
    )
    return parser.parse_args()


def render_bar(score: float, width: int = 30) -> str:
    """ASCII bar to visualize a 0-100 score."""
    filled = int((score / 100.0) * width)
    return f"[{'#' * filled}{'.' * (width - filled)}]"


def risk_label(score: float, lang: str = "en") -> str:
    """Human-readable qualitative label for a numeric risk score."""
    if score >= 80:
        return _t("risk_very_high", lang)
    if score >= 60:
        return _t("risk_high", lang)
    if score >= 40:
        return _t("risk_moderate", lang)
    if score >= 20:
        return _t("risk_low", lang)
    return _t("risk_very_low", lang)


def render_text_result(result, lang: str = "en") -> str:
    """Pretty-print risk assessment in a text-first dashboard layout."""
    lines = []
    headline = f"{result.product.name} ({result.product.ean})"
    if result.product.brand:
        headline += f" · {result.product.brand}"
    worst = result.worst_offender()

    # Quick view (what users see first)
    lines.append(_t("quick_view", lang))
    lines.append(headline)
    lines.append(
        f"{_t('total_risk', lang)}: {result.total_score:.1f}/100 ({risk_label(result.total_score, lang)}) "
        f"{render_bar(result.total_score)}"
    )
    if worst:
        lines.append(
            f"{_t('highest_concern', lang)}: {_display_name(worst.allergen_code, lang)} {worst.score:.1f}/100 "
            f"({risk_label(worst.score, lang)})"
        )

    # Ingredients and declared allergens/traces
    ingredients = _extract_ingredients(result.product.raw_payload or {})
    if ingredients:
        lines.append(f"\n{_t('section_ingredients', lang)}:")
        lines.append(f"  {', '.join(ingredients)}")

    facts = result.product.allergen_facts
    contains = [fact for fact in facts if fact.presence_type.value == "contains"]
    may = [fact for fact in facts if fact.presence_type.value == "may_contain"]
    facility = [fact for fact in facts if fact.presence_type.value == "facility_risk"]

    def _format_fact(fact):
        label = _display_name(fact.allergen_code, lang)
        presence = _presence_label(fact.presence_type.value, lang)
        return f"{label} ({presence}, source: {fact.source})"

    if contains:
        lines.append(f"\n{_t('section_contains', lang)}:")
        for fact in contains:
            lines.append(f"  - {_format_fact(fact)}")
    if may:
        lines.append(f"\n{_t('section_may_contain', lang)}:")
        for fact in may:
            lines.append(f"  - {_format_fact(fact)}")
    if facility:
        lines.append(f"\n{_t('section_facility', lang)}:")
        for fact in facility:
            lines.append(f"  - {_format_fact(fact)}")

    # Detailed reasoning for expert users
    lines.append("\n" + _t("details", lang))
    lines.append(_t("per_allergen_breakdown", lang))
    for code, detail in _sorted_details(result.per_allergen):
        reasons = "; ".join(detail.reasons)
        lines.append(
            f"  - {_display_name(code, lang)}: {detail.score:.1f}/100 ({risk_label(detail.score, lang)}) "
            f"{render_bar(detail.score)} | {reasons}"
        )
    return "\n".join(lines)


def _sorted_details(per_allergen: Dict[str, object]) -> Iterable[Tuple[str, object]]:
    """Sort allergens by descending score then code for deterministic output."""
    return sorted(per_allergen.items(), key=lambda kv: (-kv[1].score, kv[0]))


def _display_name(code: str, lang: str) -> str:
    label = allergen_label(code, lang=lang)
    if label and label.lower() != code.lower():
        return f"{code} ({label})"
    return code


def _presence_label(presence: str, lang: str) -> str:
    key = f"presence_{presence}"
    return _t(key, lang)


def _extract_ingredients(raw_payload: dict) -> list:
    """
    Collect ingredient strings from OpenFoodFacts payload (multiple languages + structured list).
    """
    texts = []
    for key in (
        "ingredients_text_" + raw_payload.get("lang", "en"),
        "ingredients_text_en",
        "ingredients_text",
        "ingredients_text_fr",
        "ingredients_text_es",
    ):
        text = raw_payload.get(key)
        if text:
            texts.append(str(text))
    ingredients_list = raw_payload.get("ingredients") or []
    for ing in ingredients_list:
        if isinstance(ing, dict) and ing.get("text"):
            texts.append(str(ing["text"]))
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for t in texts:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def _next_history_id(path: Path) -> int:
    """Return the next numeric ID for the history CSV."""
    if not path.exists():
        return 1
    last_id = 0
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                last_id = max(last_id, int(row.get("id", 0)))
            except ValueError:
                continue
    return last_id + 1


def append_history(
    args: argparse.Namespace, result, lang: str, command_label: str = "cli"
) -> None:
    """Persist the last assessment to a simple CSV audit log with richer context."""
    history_path = Path("db/history/history.csv")
    history_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "id",
        "ean",
        "user_restrictions",
        "command",
        "lang",
        "product_name",
        "brand",
        "total_risk",
        "allergen_breakdown",
        "ingredients_snapshot",
    ]

    row = {
        "id": _next_history_id(history_path),
        "ean": args.ean,
        "user_restrictions": ",".join(
            [code.strip() for code in args.allergies.split(",") if code]
        ),
        "command": command_label,
        "lang": lang,
        "product_name": "",
        "brand": "",
        "total_risk": "",
        "allergen_breakdown": "",
        "ingredients_snapshot": "",
    }

    if result:
        row["product_name"] = result.product.name
        row["brand"] = result.product.brand or ""
        row["total_risk"] = f"{result.total_score:.2f}"
        per_allergen = {
            code: {
                "score": detail.score,
                "reasons": detail.reasons,
                "facts": [
                    {
                        "allergen_code": f.allergen_code,
                        "presence": f.presence_type.value,
                        "source": f.source,
                        "weight": f.weight,
                        "confidence": f.confidence,
                    }
                    for f in detail.facts
                ],
            }
            for code, detail in result.per_allergen.items()
        }
        row["allergen_breakdown"] = json.dumps(per_allergen, ensure_ascii=False)
        row["ingredients_snapshot"] = json.dumps(
            {
                "ingredients": _extract_ingredients(result.product.raw_payload or {}),
                "allergen_facts": [
                    {
                        "allergen_code": f.allergen_code,
                        "presence": f.presence_type.value,
                        "source": f.source,
                    }
                    for f in result.product.allergen_facts
                ],
            },
            ensure_ascii=False,
        )
    else:
        row["product_name"] = "NOT_FOUND"

    write_header = not history_path.exists()
    mode = "a" if history_path.exists() else "w"
    with history_path.open(mode, newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def main() -> None:
    """Entrypoint: build risk profiles, run assessment, render output, and log history."""
    args = parse_args()
    raw_codes = [code.strip() for code in args.allergies.split(",") if code.strip()]
    resolved_codes = []
    for token in raw_codes:
        resolved = resolve_allergen_code(token)
        resolved_codes.append(resolved if resolved else token.upper())

    user_profile = UserAllergyProfile(
        allergen_codes=resolved_codes,
        avoid_traces=args.avoid_traces,
        avoid_facility_risk=args.avoid_facility_risk,
    )
    food_db = None
    try:
        food_db = FoodDatabase(csv_path=args.food_db_path)
    except FileNotFoundError:
        # Optional dataset; continue without it if missing
        food_db = None

    if args.db_dsn:
        product_source = DatabaseProductSource(args.db_dsn)
        facility_profiles = []  # facility profiles come from DB via product.facilities
    else:
        product_source = OpenFoodFactsClient()
        # Example facility profile (in a real system, fetch from DB)
        facility_profiles = [
            FacilityAllergenProfile(
                facility_id=None,
                allergen_code=code,
                process_type="packed_only",
                proportion_of_products=0.2,
            )
            for code in user_profile.normalized_codes()
        ]

    engine = RiskEngine(
        product_source=product_source,
        facility_profiles=facility_profiles,
        food_database=food_db,
    )
    result = engine.assess(args.ean, user_profile)
    if not result:
        print(_t("product_not_found", args.lang))
        append_history(args, result, lang=args.lang, command_label="main_cli")
        return

    output = {
        "ean": result.product.ean,
        "product_name": result.product.name,
        "brand": result.product.brand,
        "total_score": result.total_score,
        "ingredients": _extract_ingredients(result.product.raw_payload or {}),
        "allergen_facts": [
            {
                "allergen_code": f.allergen_code,
                "presence": f.presence_type.value,
                "source": f.source,
            }
            for f in result.product.allergen_facts
        ],
        "per_allergen": {
            code: {
                "score": detail.score,
                "reasons": detail.reasons,
            }
            for code, detail in result.per_allergen.items()
        },
    }

    if args.format == "json":
        print(json.dumps(output, indent=2))
    else:
        print(render_text_result(result, lang=args.lang))

    append_history(args, result, lang=args.lang, command_label="main_cli")


if __name__ == "__main__":
    main()
