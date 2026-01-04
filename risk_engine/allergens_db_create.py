"""
Scan FoodDB CSV rows and flag ingredients that fit the 14 Annex II allergen
categories. Each output row represents one food record matched to a single
allergen category and includes a lightweight confidence rating.

Notes:
- Milk covers animal dairy only. Plant-based drinks such as soy/almond/oat milk
  are mapped to their own categories (soy/tree nuts) and explicitly excluded
  from the milk category.
"""

from __future__ import annotations

# Standard library helpers for text normalization and file paths.
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

# Pandas is used for CSV I/O and DataFrame assembly.
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
FOOD_CSV = ROOT / "db" / "foodb_2020_04_07_csv" / "Food.csv"
OUTPUT_CSV = ROOT / "db" / "allergens.csv"


def _normalize(text: str) -> str:
    """Lowercase, strip accents, and collapse whitespace for tolerant matching."""
    # Normalize accents and punctuation for matching rules.
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.lower()
    # Replace non-alphanumeric characters with spaces to make word-boundary
    # matching reliable.
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(cleaned.split())


def _word_present(text: str, needle: str) -> bool:
    """Check if needle appears as a full word/phrase in text."""
    # Use a regex with word boundaries to prevent partial matches.
    if not needle:
        return False
    pattern = rf"(?<!\w){re.escape(needle)}(?!\w)"
    return re.search(pattern, text) is not None


@dataclass
class AllergenRule:
    """Rule describing how to detect an allergen category and assign a rating."""
    code: str
    category: str
    keywords: List[Tuple[str, int]]
    exclude_phrases: List[str] = field(default_factory=list)

    def normalize(self) -> "AllergenRule":
        # Normalize all keyword and exclusion phrases for matching.
        normalized_keywords = [(_normalize(term), score) for term, score in self.keywords]
        normalized_excludes = [_normalize(term) for term in self.exclude_phrases]
        return AllergenRule(
            code=self.code,
            category=self.category,
            keywords=normalized_keywords,
            exclude_phrases=normalized_excludes,
        )


def _kw(score: int, *terms: str) -> List[Tuple[str, int]]:
    # Convenience helper for building keyword/score lists.
    return [(term, score) for term in terms]


# Plant-based milk phrases that should not count as dairy.
PLANT_MILK_PHRASES = [
    "soy milk",
    "soya milk",
    "almond milk",
    "oat milk",
    "rice milk",
    "coconut milk",
    "cashew milk",
    "pea milk",
    "hemp milk",
    "hazelnut milk",
    "macadamia milk",
    "pistachio milk",
]

# Keyword rules for each Annex II allergen category.
ALLERGEN_RULES: List[AllergenRule] = [
    AllergenRule(
        code="gluten",
        category="Cereals containing gluten",
        keywords=_kw(
            100,
            "wheat",
            "rye",
            "barley",
            "oat",
            "oats",
            "spelt",
            "triticale",
            "durum",
            "kamut",
            "gluten",
        )
        + _kw(
            85,
            "malt",
            "semolina",
            "farro",
            "bulgur",
            "seitan",
        ),
    ),
    AllergenRule(
        code="crustaceans",
        category="Crustaceans",
        keywords=_kw(
            100,
            "shrimp",
            "prawn",
            "prawns",
            "lobster",
            "crab",
            "crayfish",
            "crawfish",
            "langoustine",
            "krill",
            "crustacean",
            "crustaceans",
        ),
    ),
    AllergenRule(
        code="egg",
        category="Eggs",
        keywords=_kw(
            100,
            "egg",
            "eggs",
            "egg yolk",
            "egg white",
            "yolk",
            "albumen",
            "ovo",
        )
        + _kw(
            75,
            "mayonnaise",
            "mayo",
            "aioli",
            "meringue",
        ),
    ),
    AllergenRule(
        code="fish",
        category="Fish",
        keywords=_kw(
            100,
            "fish",
            "salmon",
            "tuna",
            "cod",
            "haddock",
            "trout",
            "mackerel",
            "anchovy",
            "anchovies",
            "sardine",
            "sardines",
            "herring",
            "halibut",
            "pollock",
            "sole",
            "snapper",
            "bass",
            "tilapia",
            "catfish",
            "carp",
        )
        + _kw(
            80,
            "fish sauce",
            "fish oil",
            "surimi",
            "omega 3 fish",
        ),
    ),
    AllergenRule(
        code="peanut",
        category="Peanuts",
        keywords=_kw(
            100,
            "peanut",
            "peanuts",
            "groundnut",
            "groundnuts",
            "arachis",
            "goober",
            "monkey nut",
        )
        + _kw(
            85,
            "peanut butter",
            "peanut oil",
        ),
    ),
    AllergenRule(
        code="soy",
        category="Soybeans",
        keywords=_kw(
            100,
            "soy",
            "soya",
            "soybean",
            "soybeans",
            "edamame",
            "tofu",
            "tempeh",
            "miso",
            "natto",
            "soy milk",
            "soya milk",
            "soy sauce",
        ),
    ),
    AllergenRule(
        code="milk",
        category="Milk",
        keywords=_kw(
            100,
            "milk",
            "cow milk",
            "goat milk",
            "sheep milk",
            "dairy",
            "lactose",
            "whey",
            "casein",
            "buttermilk",
        )
        + _kw(
            90,
            "cheese",
            "cream",
            "yogurt",
            "yoghurt",
            "kefir",
            "paneer",
        )
        + _kw(
            75,
            "butter",
            "ghee",
        ),
        exclude_phrases=PLANT_MILK_PHRASES,
    ),
    AllergenRule(
        code="tree_nuts",
        category="Nuts",
        keywords=_kw(
            100,
            "almond",
            "hazelnut",
            "walnut",
            "cashew",
            "pecan",
            "brazil nut",
            "pistachio",
            "macadamia",
            "pine nut",
        )
        + _kw(
            90,
            "almond flour",
            "almond milk",
            "hazelnut milk",
            "cashew milk",
            "pistachio milk",
            "nut butter",
            "praline",
            "marzipan",
        ),
    ),
    AllergenRule(
        code="celery",
        category="Celery",
        keywords=_kw(
            100,
            "celery",
            "celeriac",
            "aipo",
        ),
    ),
    AllergenRule(
        code="mustard",
        category="Mustard",
        keywords=_kw(
            100,
            "mustard",
            "mustard seed",
            "mustard seeds",
            "mustard powder",
            "dijon",
        ),
    ),
    AllergenRule(
        code="sesame",
        category="Sesame seeds",
        keywords=_kw(
            100,
            "sesame",
            "sesame seed",
            "sesame seeds",
            "tahini",
            "tahina",
            "gomasio",
        ),
    ),
    AllergenRule(
        code="sulphites",
        category="Sulphur dioxide and sulphites",
        keywords=_kw(
            100,
            "sulphite",
            "sulphites",
            "sulfite",
            "sulfites",
            "sulphur dioxide",
            "sulfur dioxide",
            "sodium metabisulfite",
            "potassium metabisulfite",
            "e220",
            "e221",
            "e222",
            "e223",
            "e224",
            "e226",
            "e227",
            "e228",
        ),
    ),
    AllergenRule(
        code="lupin",
        category="Lupin",
        keywords=_kw(
            100,
            "lupin",
            "lupine",
            "lupini",
            "tremoco",
            "tremocos",
        ),
    ),
    AllergenRule(
        code="molluscs",
        category="Molluscs",
        keywords=_kw(
            100,
            "mollusc",
            "molluscs",
            "mollusk",
            "mollusks",
            "oyster",
            "oysters",
            "mussel",
            "mussels",
            "clam",
            "clams",
            "cockle",
            "scallop",
            "squid",
            "calamari",
            "octopus",
            "whelk",
            "abalone",
            "snail",
            "snails",
        ),
    ),
]

# Normalize all rules once so detection runs quickly.
ALLERGEN_RULES = [rule.normalize() for rule in ALLERGEN_RULES]


def _strip_excludes(text: str, excludes: Iterable[str]) -> str:
    """Remove excluded phrases before matching to avoid false positives."""
    # Remove excluded phrases like "soy milk" before searching for "milk".
    stripped = text
    for phrase in excludes:
        if not phrase:
            continue
        stripped = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", stripped)
    return " ".join(stripped.split())


def detect_allergens_in_fields(fields: List[str]) -> List[Tuple[str, str, int]]:
    """
    Return a list of (code, category, rating) matches for the provided text fields.
    A rating is the maximum score among matched keywords for that category.
    """
    # Normalize each input field before matching.
    normalized_fields = [_normalize(value) for value in fields if value]
    best_scores: Dict[str, Tuple[str, int]] = {}

    for field_text in normalized_fields:
        for rule in ALLERGEN_RULES:
            # Remove excluded phrases then scan for any rule keywords.
            processed_text = _strip_excludes(field_text, rule.exclude_phrases)
            score = 0
            for phrase, value in rule.keywords:
                if _word_present(processed_text, phrase):
                    score = max(score, value)
            if score:
                current = best_scores.get(rule.code, (rule.category, 0))
                if score > current[1]:
                    best_scores[rule.code] = (rule.category, score)

    # Convert the best scores into a compact list of matches.
    matches: List[Tuple[str, str, int]] = []
    for code, (category, rating) in best_scores.items():
        if rating:
            matches.append((code, category, rating))
    return matches


def main() -> None:
    # Load the FoodDB dataset and scan for allergen keyword hits.
    df = pd.read_csv(FOOD_CSV)
    matches = []

    for _, row in df.iterrows():
        # Use name and scientific name as high-confidence sources. Descriptions
        # are intentionally ignored to avoid false positives like "egg-sized fruit".
        text_fields = [
            str(row.get("name", "")),
            str(row.get("name_scientific", "")),
        ]

        hits = detect_allergens_in_fields(text_fields)
        if not hits:
            continue

        # Produce one output row per allergen hit for downstream consumption.
        base_row = row.to_dict()
        for code, category, rating in hits:
            row_data = dict(base_row)
            row_data["allergen_code"] = code
            row_data["allergen_rating"] = rating
            # Keep the legacy columns for compatibility with downstream code.
            row_data["allergens_detected"] = code
            row_data["allergen_category"] = category
            matches.append(row_data)

    # Write results to a CSV compatible with the rest of the pipeline.
    allergens_df = pd.DataFrame(matches)
    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    allergens_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8")

    # Emit a short summary for CLI usage.
    print(f"Done! Found {len(allergens_df)} allergen rows.")
    print(f"Saved to {OUTPUT_CSV}")


if __name__ == "__main__":
    # Allow running as a script.
    main()
