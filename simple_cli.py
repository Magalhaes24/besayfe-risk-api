"""
Interactive helper to run the risk engine without remembering flags.
Workflow:
- Ask language first so all prompts/output localize correctly.
- Prompt for EAN, allergen selection via numbered Annex II list, and risk flags.
- Build UserAllergyProfile, fetch from OpenFoodFacts, optionally enrich with FoodDB,
  run RiskEngine, print the formatted report, and log history.

Usage:
    python simple_cli.py
"""
import argparse
from pathlib import Path
from typing import Optional

from risk_engine import (
    FoodDatabase,
    ImageTextProductSource,
    OpenFoodFactsClient,
    RiskEngine,
    UserAllergyProfile,
)
from risk_engine.allergens import ANNEX_II_ALLERGENS
from main import _t, append_history, render_text_result


def prompt_bool(label: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    resp = input(f"{label} {suffix}: ").strip().lower()
    if not resp:
        return default
    return resp.startswith("y")


def prompt_allergens(lang: str = "en") -> list[str]:
    """
    Console-friendly "dropdown": show the 14 Annex II options and let the user pick by number.
    Returns canonical allergen codes (e.g. MILK, GLUTEN).
    """
    options = list(ANNEX_II_ALLERGENS.items())
    print("\n" + _t("select_allergens", lang))
    for idx, (code, meta) in enumerate(options, start=1):
        label = meta.get(lang) or meta.get("en") or code.title()
        print(f"  {idx:2d}. {label} [{code}]")

    while True:
        raw = input(_t("selection_prompt", lang)).strip()
        if not raw:
            print(_t("select_error_empty", lang))
            continue
        try:
            indices = [
                int(token)
                for token in raw.replace(" ", "").split(",")
                if token.strip()
            ]
        except ValueError:
            print(_t("select_error_numbers", lang))
            continue

        invalid = [i for i in indices if i < 1 or i > len(options)]
        if invalid:
            print(_t("select_error_range", lang).format(invalid=invalid))
            continue

        codes = [options[i - 1][0] for i in indices]
        return codes


def prompt_input_mode(lang: str = "en") -> str:
    """Ask whether to assess by barcode or image input."""
    while True:
        raw = input("Input type [barcode/image]: ").strip().lower()
        if raw in ("barcode", "b", "ean"):
            return "barcode"
        if raw in ("image", "img", "i"):
            return "image"
        print("Please enter 'barcode' or 'image'.")


def prompt_image_path() -> Path:
    """Prompt until the user provides a readable image path."""
    while True:
        raw = input("Image file path: ").strip().strip('"')
        if not raw:
            print("Please enter a file path.")
            continue
        path = Path(raw)
        if not path.exists() or not path.is_file():
            print("File not found. Please enter a valid image file path.")
            continue
        return path


def main() -> None:
    lang = input(_t("prompt_language", "en")).strip() or "en"
    print(_t("cli_title", lang))
    input_mode = prompt_input_mode(lang=lang)
    ean = ""
    image_path: Optional[Path] = None
    if input_mode == "barcode":
        ean = input(_t("prompt_ean", lang)).strip()
    else:
        image_path = prompt_image_path()
    allergy_codes = prompt_allergens(lang=lang)
    avoid_traces = prompt_bool(_t("prompt_may_contain", lang), default=True)
    avoid_facility = prompt_bool(_t("prompt_facility", lang), default=False)

    user_profile = UserAllergyProfile(
        allergen_codes=allergy_codes,
        avoid_traces=avoid_traces,
        avoid_facility_risk=avoid_facility,
    )

    # Try to load the bundled FoodDB for richer signals if available.
    food_db = None
    default_food_path = Path("db/foodb_2020_04_07_csv/Food.csv")
    if default_food_path.exists():
        try:
            food_db = FoodDatabase(csv_path=str(default_food_path))
        except FileNotFoundError:
            food_db = None

    client = OpenFoodFactsClient()
    engine = RiskEngine(product_source=client, food_database=food_db)

    if input_mode == "barcode":
        result = engine.assess(ean=ean, user_profile=user_profile)
    else:
        ocr_source = ImageTextProductSource(lang="eng")
        image_bytes = image_path.read_bytes() if image_path else b""
        product = ocr_source.product_from_image(
            image_bytes=image_bytes,
            reference_id=image_path.name if image_path else "image-input",
            name=image_path.name if image_path else "Image input",
        )
        result = engine.assess_product(product, user_profile=user_profile)
    if not result:
        print(_t("product_not_found", lang))
        return

    print()
    print(render_text_result(result, lang=lang))

    # Persist a history row matching the main CLI format for consistency.
    args_namespace = argparse.Namespace(
        ean=ean or (image_path.name if image_path else "image-input"),
        allergies=",".join(allergy_codes),
    )
    append_history(
        args_namespace,
        result,
        lang=lang,
        command_label="simple_cli",
        request_source="local" if input_mode == "barcode" else "image",
    )


if __name__ == "__main__":
    main()
