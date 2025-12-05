"""
Data source implementation for OpenFoodFacts.
Fetches product JSON, normalizes declared allergens and "may contain" tags into
AllergenFact entries, and exposes a ProductInfo for the risk engine.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import requests

from .allergens import OFF_TAG_TO_CODE
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
        self.session = session or requests.Session()
        self.timeout = timeout
        self.log = logging.getLogger(self.__class__.__name__)

    def get_product(self, ean: str) -> Optional[ProductInfo]:
        try:
            response = self.session.get(
                self.BASE_URL.format(ean=ean), timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # pragma: no cover - network errors handled at runtime
            self.log.warning("OpenFoodFacts fetch failed for %s: %s", ean, exc)
            return None

        if not data or data.get("status") != 1:
            self.log.info("Product %s not found on OpenFoodFacts", ean)
            return None

        product_data = data.get("product", {})
        allergens_tags: List[str] = product_data.get("allergens_tags", []) or []
        ingredients_analysis: List[str] = product_data.get(
            "ingredients_analysis_tags", []
        ) or []
        traces_tags: List[str] = product_data.get("traces_tags", []) or []

        facts = self._build_allergen_facts(allergens_tags, ingredients_analysis)

        return ProductInfo(
            ean=ean,
            name=product_data.get("product_name", "Unknown product"),
            brand=(product_data.get("brands") or "").split(",")[0].strip()
            or None,
            source="openfoodfacts",
            allergen_facts=facts,
            raw_payload=product_data,
            traces_tags=traces_tags,
        )

    def _build_allergen_facts(
        self, allergens_tags: List[str], ingredient_analysis_tags: List[str]
    ) -> List[AllergenFact]:
        facts: List[AllergenFact] = []

        # Explicit allergens field means declared presence
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

        # Ingredient analysis can include "may contain" hints
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
    def _tag_to_code(tag: str) -> Optional[str]:
        normalized = tag.lower()
        if ":may-contain-" in normalized:
            lang, suffix = normalized.split(":may-contain-", 1)
            normalized = f"{lang}:{suffix}"
        return OpenFoodFactsClient.OFF_TO_INTERNAL.get(normalized)
