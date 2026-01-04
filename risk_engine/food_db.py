"""
FoodDB enrichment helpers.

Loads FoodDB CSV records, filters to relevant allergen-bearing categories, and
uses keyword rules to infer allergen facts from ingredient text.
"""

from __future__ import annotations

# Standard library helpers for CSV parsing, paths, and tokenization.
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import AllergenFact, PresenceType, ProductInfo


@dataclass(frozen=True)
class FoodRecord:
    """Normalized FoodDB row for ingredient group matching."""
    id: str
    name: str
    description: str
    food_group: str
    food_subgroup: str
    food_type: str

    def summary(self) -> str:
        # Provide a compact group/subgroup label for diagnostics.
        subgroup = self.food_subgroup or "unknown subgroup"
        group = self.food_group or "unknown group"
        return f"{group}/{subgroup}"


class FoodDatabase:
    """
    Lightweight reader for FoodDB CSV files to enrich allergen detection.
    It matches ingredients against Food.csv entries and maps food groups to
    allergen codes (including cross-reactive families like pulses -> peanuts).
    """

    DEFAULT_CSV_PATH = (
        Path(__file__).resolve().parent.parent / "db" / "foodb_2020_04_07_csv" / "Food.csv"
    )

    # keyword -> list of (allergen_code, weight, confidence, rationale_suffix)
    KEYWORD_RULES: Dict[str, List[Tuple[str, float, float, str]]] = {
        # Nuts and close relatives
        "peanut": [("PEANUT", 1.0, 0.95, "peanut detected")],
        "groundnut": [("PEANUT", 1.0, 0.9, "groundnut detected")],
        "nut": [("TREE_NUTS", 0.8, 0.8, "nut family ingredient")],
        "nuts": [("TREE_NUTS", 0.85, 0.8, "nut family ingredient")],
        "almond": [("TREE_NUTS", 1.0, 0.95, "almond ingredient")],
        "cashew": [("TREE_NUTS", 1.0, 0.95, "cashew ingredient")],
        "walnut": [("TREE_NUTS", 1.0, 0.95, "walnut ingredient")],
        "pecan": [("TREE_NUTS", 1.0, 0.9, "pecan ingredient")],
        "hazelnut": [("TREE_NUTS", 1.0, 0.9, "hazelnut ingredient")],
        "pistachio": [("TREE_NUTS", 1.0, 0.9, "pistachio ingredient")],
        "macadamia": [("TREE_NUTS", 1.0, 0.9, "macadamia ingredient")],
        "lupin": [
            ("LUPIN", 1.0, 0.9, "lupin ingredient"),
            ("PEANUT", 0.55, 0.65, "pulse cousin of peanuts"),
            ("TREE_NUTS", 0.45, 0.6, "pulse cousin of nuts"),
        ],
        "lupine": [
            ("LUPIN", 1.0, 0.9, "lupine ingredient"),
            ("PEANUT", 0.55, 0.65, "pulse cousin of peanuts"),
            ("TREE_NUTS", 0.45, 0.6, "pulse cousin of nuts"),
        ],
        "pulse": [
            ("PEANUT", 0.7, 0.65, "pulse/legume family"),
            ("SOY", 0.6, 0.6, "pulse/legume family"),
            ("TREE_NUTS", 0.45, 0.55, "pulse/legume cousin"),
        ],
        "pulses": [
            ("PEANUT", 0.7, 0.65, "pulse/legume family"),
            ("SOY", 0.6, 0.6, "pulse/legume family"),
            ("TREE_NUTS", 0.45, 0.55, "pulse/legume cousin"),
        ],
        "legume": [
            ("PEANUT", 0.7, 0.65, "legume family"),
            ("SOY", 0.6, 0.6, "legume family"),
            ("TREE_NUTS", 0.4, 0.5, "legume cousin"),
        ],
        "legumes": [
            ("PEANUT", 0.7, 0.65, "legume family"),
            ("SOY", 0.6, 0.6, "legume family"),
            ("TREE_NUTS", 0.4, 0.5, "legume cousin"),
        ],
        "soy": [("SOY", 1.0, 0.95, "soy ingredient")],
        "soybean": [("SOY", 1.0, 0.95, "soy ingredient")],
        "soybeans": [("SOY", 1.0, 0.95, "soy ingredient")],
        # Seeds
        "sesame": [("SESAME", 1.0, 0.95, "sesame seed ingredient")],
        # Gluten-bearing cereals
        "wheat": [("GLUTEN", 1.0, 0.95, "gluten cereal (wheat)")],
        "barley": [("GLUTEN", 1.0, 0.95, "gluten cereal (barley)")],
        "rye": [("GLUTEN", 1.0, 0.9, "gluten cereal (rye)")],
        "spelt": [("GLUTEN", 1.0, 0.9, "gluten cereal (spelt)")],
        "oat": [("GLUTEN", 0.45, 0.75, "cereal (oat)")],
        "oats": [("GLUTEN", 0.45, 0.75, "cereal (oats)")],
        "cereal": [("GLUTEN", 0.7, 0.65, "cereal/grain family")],
        "grain": [("GLUTEN", 0.6, 0.6, "cereal/grain family")],
        # Animal products
        "milk": [("MILK", 1.0, 0.95, "milk/dairy ingredient")],
        "dairy": [("MILK", 0.9, 0.85, "dairy ingredient")],
        "cheese": [("MILK", 0.85, 0.8, "cheese (dairy)")],
        "casein": [("MILK", 0.9, 0.85, "casein (milk protein)")],
        "egg": [("EGG", 1.0, 0.95, "egg ingredient")],
        "eggs": [("EGG", 1.0, 0.95, "egg ingredient")],
        "fish": [("FISH", 1.0, 0.95, "fish ingredient")],
        "salmon": [("FISH", 0.9, 0.9, "fish ingredient (salmon)")],
        "tuna": [("FISH", 0.9, 0.9, "fish ingredient (tuna)")],
        # Condiments
        "mustard": [("MUSTARD", 1.0, 0.95, "mustard ingredient")],
    }

    RELEVANT_GROUP_KEYWORDS: Tuple[str, ...] = (
        "nut",
        "pulse",
        "legume",
        "oilseed",
        "cereal",
        "grain",
        "dairy",
        "milk",
        "egg",
        "fish",
        "seed",
    )

    def __init__(self, csv_path: Optional[str] = None, preload: bool = True):
        # Store paths and initialize the in-memory index.
        self.csv_path = Path(csv_path) if csv_path else self.DEFAULT_CSV_PATH
        self.records: List[FoodRecord] = []
        self.token_index: Dict[str, List[FoodRecord]] = {}
        self.loaded = False
        # Optionally preload FoodDB records at startup.
        if preload and self.csv_path.exists():
            self._load()

    def _load(self) -> None:
        # Build the record list and token index from the CSV.
        self.records.clear()
        self.token_index.clear()
        with self.csv_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                # Normalize CSV rows into FoodRecord entries.
                record = FoodRecord(
                    id=row.get("id", ""),
                    name=row.get("name", ""),
                    description=row.get("description", ""),
                    food_group=row.get("food_group", ""),
                    food_subgroup=row.get("food_subgroup", ""),
                    food_type=row.get("food_type", ""),
                )
                # Skip records that do not appear relevant to allergen detection.
                if not self._is_relevant(record):
                    continue
                self.records.append(record)
                # Build a token index for quick matching.
                for token in self._tokens_for_record(record):
                    if token not in self.token_index:
                        self.token_index[token] = []
                    self.token_index[token].append(record)
        self.loaded = True

    def infer_allergen_facts(
        self, product: ProductInfo, user_allergen_codes: Iterable[str]
    ) -> List[AllergenFact]:
        """
        Analyze product ingredient text and add allergen facts using FoodDB relationships.
        Only allergen codes present in user_allergen_codes are emitted to avoid noise.
        """
        # Bail out early if the FoodDB dataset has not been loaded.
        if not self.loaded:
            return []
        raw = product.raw_payload or {}
        ingredient_texts = self._collect_ingredient_texts(raw)
        if not ingredient_texts:
            return []

        # Tokenize ingredient text and restrict to the user's tracked allergens.
        normalized_codes = {code.upper() for code in user_allergen_codes}
        tokens: Set[str] = set()
        for text in ingredient_texts:
            tokens.update(self._tokenize(text))

        # Apply keyword rules to generate inferred allergen facts.
        facts: List[AllergenFact] = []
        for token in tokens:
            for code, weight, confidence, _reason in self.KEYWORD_RULES.get(token, []):
                if code not in normalized_codes:
                    continue
                if code == "MILK" and self._is_plant_based_milk_token(tokens):
                    # Avoid treating plant-based milks (e.g., soy/almond/oat milk) as dairy allergens
                    # when no dairy markers are present.
                    continue
                # Emit a fact for each matching keyword rule.
                facts.append(
                    AllergenFact(
                        allergen_code=code,
                        presence_type=PresenceType.CONTAINS,
                        source=f"foodb:keyword:{token}",
                        weight=weight,
                        confidence=confidence,
                    )
                )
        return facts

    def _collect_ingredient_texts(self, raw: Dict) -> List[str]:
        # Gather ingredient text fields from the raw payload.
        texts: List[str] = []
        for key in (
            "ingredients_text_en",
            "ingredients_text",
            "ingredients_text_fr",
            "ingredients_text_es",
        ):
            text = raw.get(key)
            if text:
                texts.append(str(text))
        # Add text from structured ingredient objects.
        ingredients_list = raw.get("ingredients") or []
        for ing in ingredients_list:
            if not isinstance(ing, dict):
                continue
            text = ing.get("text")
            if text:
                texts.append(str(text))
        return texts

    def _tokens_for_record(self, record: FoodRecord) -> Set[str]:
        # Tokenize record fields to build the lookup index.
        tokens = set()
        for field in (
            record.name,
            record.food_group,
            record.food_subgroup,
            record.food_type,
        ):
            tokens.update(self._tokenize(field))
        return tokens

    def _tokens_from_text(self, text: str) -> List[str]:
        # Normalize and split text into tokens longer than 2 characters.
        normalized = self._normalize(text)
        return [tok for tok in normalized.split() if len(tok) > 2]

    def _tokenize(self, text: str) -> List[str]:
        # Alias to keep tokenization logic centralized.
        return self._tokens_from_text(text)

    def _normalize(self, text: str) -> str:
        # Lowercase and strip non-alphanumeric characters for uniform matching.
        return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

    def _is_relevant(self, record: FoodRecord) -> bool:
        # Check whether the record's categories include allergen-related keywords.
        haystack = f"{record.food_group} {record.food_subgroup} {record.name}".lower()
        return any(keyword in haystack for keyword in self.RELEVANT_GROUP_KEYWORDS)

    @staticmethod
    def _is_plant_based_milk_token(tokens: Set[str]) -> bool:
        # Detect "milk" entries that are clearly plant-based to avoid dairy false positives.
        if "milk" not in tokens:
            return False
        plant_markers = {
            "soy",
            "soya",
            "almond",
            "oat",
            "rice",
            "coconut",
            "hazelnut",
            "pea",
            "cashew",
        }
        dairy_markers = {"lactose", "whey", "casein", "butter", "cheese", "cream", "yogurt", "yoghurt"}
        has_plant = any(marker in tokens for marker in plant_markers)
        has_dairy = any(marker in tokens for marker in dairy_markers)
        return has_plant and not has_dairy
