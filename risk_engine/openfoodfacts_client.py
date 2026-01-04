"""
Data source implementation for OpenFoodFacts.
Fetches product JSON, normalizes declared allergens and "may contain" tags into
AllergenFact entries, enriches with ingredients-based allergen detection, and
exposes a ProductInfo for the risk engine.
"""

from __future__ import annotations

# Standard library logging and typing helpers.
import logging
from typing import Dict, List, Optional

# HTTP client for OpenFoodFacts API access.
import requests

# Allergen parsing utilities and domain models.
from .allergens import OFF_TAG_TO_CODE, detect_allergens_in_ingredient_texts
from .models import AllergenFact, PresenceType, ProductInfo


class ProductDataSource:
    """
    Base interface for any product data source (DB, API, cache).
    """

    def get_product(self, ean: str) -> Optional[ProductInfo]:
        raise NotImplementedError


class OpenFoodFactsClient(ProductDataSource):
    """
    Thin wrapper around OpenFoodFacts public API to standardize product info.
    """

    BASE_URL = "https://world.openfoodfacts.org/api/v0/product/{ean}.json"
    OFF_TO_INTERNAL = OFF_TAG_TO_CODE

    def __init__(self, session: Optional[requests.Session] = None, timeout: float = 5.0):
        # Configure the HTTP client and default timeout.
        self.session = session or requests.Session()
        self.timeout = timeout
        self.log = logging.getLogger(self.__class__.__name__)

    def get_product(self, ean: str) -> Optional[ProductInfo]:
        # Fetch JSON from OpenFoodFacts with basic error handling.
        try:
            response = self.session.get(
                self.BASE_URL.format(ean=ean), timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # pragma: no cover - network errors handled at runtime
            self.log.warning("OpenFoodFacts fetch failed for %s: %s", ean, exc)
            return None

        # Validate the response payload for a successful product lookup.
        if not data or data.get("status") != 1:
            self.log.info("Product %s not found on OpenFoodFacts", ean)
            return None

        # Pull primary fields and normalized ingredient text from the payload.
        product_data = data.get("product", {})
        allergens_tags: List[str] = product_data.get("allergens_tags", []) or []
        ingredients_analysis: List[str] = product_data.get(
            "ingredients_analysis_tags", []
        ) or []
        traces_tags: List[str] = product_data.get("traces_tags", []) or []
        ingredient_texts = self._collect_ingredient_texts(product_data)

        # Build allergen facts from declared tags and ingredient inference.
        facts = self._build_allergen_facts(allergens_tags, ingredients_analysis)
        ingredient_facts = self._facts_from_ingredients(ingredient_texts)
        facts = self._merge_facts(facts, ingredient_facts)

        # Track data quality so callers can distinguish missing data vs. clean analysis.
        data_notes: List[str] = []
        if not ingredient_texts:
            data_notes.append(
                "No ingredient information available; insufficient data to compute ingredient-based risk"
            )
        if not allergens_tags and not ingredient_texts:
            data_notes.append(
                "OpenFoodFacts entry has no declared allergens or ingredient text; cannot compute risk from source data"
            )
        elif ingredient_texts and not facts:
            data_notes.append(
                "Ingredients analyzed and no allergens detected in available data"
            )

        # Convert the payload into the standardized ProductInfo model.
        return ProductInfo(
            ean=ean,
            name=product_data.get("product_name", "Unknown product"),
            brand=(product_data.get("brands") or "").split(",")[0].strip()
            or None,
            source="openfoodfacts",
            allergen_facts=facts,
            raw_payload=product_data,
            traces_tags=traces_tags,
            data_notes=data_notes,
        )

    def _build_allergen_facts(
        self, allergens_tags: List[str], ingredient_analysis_tags: List[str]
    ) -> List[AllergenFact]:
        facts: List[AllergenFact] = []

        # Explicit allergens field means declared presence.
        for tag in allergens_tags:
            code = self._tag_to_code(tag)
            if not code:
                continue
            facts.append(
                AllergenFact(
                    allergen_code=code,
                    presence_type=PresenceType.CONTAINS,
                    source="openfoodfacts:allergens",
                    weight=1.0,
                    # Declared allergens on the label are definitive; use full confidence.
                    confidence=1.0,
                )
            )

        # Ingredient analysis can include "may contain" hints.
        for tag in ingredient_analysis_tags:
            if "may-contain" not in tag.lower():
                continue
            code = self._tag_to_code(tag)
            if not code:
                continue
            facts.append(
                AllergenFact(
                    allergen_code=code,
                    presence_type=PresenceType.MAY_CONTAIN,
                    source="openfoodfacts:ingredients_analysis",
                    weight=0.6,
                    confidence=0.6,
                )
            )

        return facts

    @staticmethod
    def _collect_ingredient_texts(payload: Dict) -> List[str]:
        """
        Gather ingredient text fields and structured ingredient entries from OFF payload.
        """
        texts: List[str] = []
        # Collect text from multiple language variants.
        for key in (
            "ingredients_text_en",
            "ingredients_text",
            "ingredients_text_fr",
            "ingredients_text_es",
        ):
            text = payload.get(key)
            if text:
                texts.append(str(text))
        # Collect text from the structured ingredients list.
        ingredients_list = payload.get("ingredients") or []
        for ing in ingredients_list:
            if isinstance(ing, dict) and ing.get("text"):
                texts.append(str(ing["text"]))
        return texts

    def _facts_from_ingredients(self, ingredient_texts: List[str]) -> List[AllergenFact]:
        """
        Parse ingredient text to detect allergens that might be missing from OFF allergen tags.
        """
        facts: List[AllergenFact] = []
        # Use the allergen detector to map ingredient tokens to codes.
        detected_codes = detect_allergens_in_ingredient_texts(ingredient_texts)
        for code in detected_codes:
            facts.append(
                AllergenFact(
                    allergen_code=code,
                    presence_type=PresenceType.CONTAINS,
                    source="openfoodfacts:ingredients_text",
                    weight=1.0,
                    confidence=0.85,
                )
            )
        return facts

    def _merge_facts(
        self, primary: List[AllergenFact], secondary: List[AllergenFact]
    ) -> List[AllergenFact]:
        """
        Combine fact lists while avoiding duplicate allergen/presence combinations.
        """
        # Track seen (code, presence_type) pairs to dedupe facts.
        existing = {(f.allergen_code, f.presence_type) for f in primary}
        merged = list(primary)
        for fact in secondary:
            key = (fact.allergen_code, fact.presence_type)
            if key in existing:
                continue
            existing.add(key)
            merged.append(fact)
        return merged

    @staticmethod
    def _tag_to_code(tag: str) -> Optional[str]:
        # Normalize OFF "may-contain" tags to the main tag format.
        normalized = tag.lower()
        if ":may-contain-" in normalized:
            lang, suffix = normalized.split(":may-contain-", 1)
            normalized = f"{lang}:{suffix}"
        return OpenFoodFactsClient.OFF_TO_INTERNAL.get(normalized)
