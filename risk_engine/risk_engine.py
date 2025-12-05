"""
Central risk engine: pulls product data from a source, enriches with optional
FoodDB signals, filters by user preferences, and rolls up per-allergen and total
risk scores (0-100).

Key stages:
- fetch product (and its declared allergens) from an injected data source
  (OpenFoodFacts or DB)
- optionally enrich with facility profiles and FoodDB ingredient inference
- honor user flags for traces and facility risk
- aggregate multiple facts per allergen into a single score
- compute an overall score using complementary probability to avoid overcounting
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .models import (
    AllergenFact,
    FacilityAllergenProfile,
    PresenceType,
    ProductInfo,
    RiskDetail,
    RiskResult,
    UserAllergyProfile,
)
from .openfoodfacts_client import ProductDataSource
from .food_db import FoodDatabase
from .cross_contact_bhm import final_cross_contact_risk


class RiskEngine:
    """
    Orchestrates fetching data, standardizing it, and computing a 0-100 risk score.
    Inject different data sources or facility profiles to adapt to your stack.
    """

    def __init__(
        self,
        product_source: ProductDataSource,
        facility_profiles: Optional[Iterable[FacilityAllergenProfile]] = None,
        fallback_score: float = 5.0,
        food_database: Optional[FoodDatabase] = None,
    ):
        self.product_source = product_source
        self.facility_profiles = list(facility_profiles or [])
        self.fallback_score = fallback_score
        self.food_database = food_database

    def assess(
        self, ean: str, user_profile: UserAllergyProfile
    ) -> Optional[RiskResult]:
        """
        Fetch a product by EAN, enrich and filter allergen facts, and return
        per-allergen and total risk scores respecting user preferences.
        """
        product = self.product_source.get_product(ean)
        if not product:
            return None

        # Attach facility-based facts if we have them
        facility_facts = self._facility_facts_for_product(product)
        product.allergen_facts.extend(facility_facts)
        if self.food_database:
            enriched_facts = self.food_database.infer_allergen_facts(
                product, user_profile.normalized_codes()
            )
            product.allergen_facts.extend(enriched_facts)

        per_allergen: Dict[str, RiskDetail] = {}
        normalized_codes = user_profile.normalized_codes()

        for code in normalized_codes:
            facts = [
                fact
                for fact in product.allergen_facts
                if fact.allergen_code == code
                and self._include_fact(fact, user_profile)
            ]

            # Inject Bayesian facility cross-contact fact if requested
            if user_profile.avoid_facility_risk:
                bhm_fact = self._bhm_cross_contact_fact(product, code)
                if bhm_fact:
                    facts.append(bhm_fact)

            if facts:
                scores = [fact.normalized_score() for fact in facts]
                score = self._aggregate_scores(scores)
                reasons = [
                    f"{fact.presence_type.value} via {fact.source} "
                    f"(w={fact.weight}, conf={fact.confidence})"
                    for fact in facts
                ]
            else:
                score = self.fallback_score
                reasons = ["No direct data; applying conservative fallback"]

            per_allergen[code] = RiskDetail(
                allergen_code=code, score=score, reasons=reasons, facts=facts
            )

        total_score = self._aggregate_scores([d.score for d in per_allergen.values()])

        return RiskResult(
            total_score=round(total_score, 2),
            product=product,
            per_allergen=per_allergen,
        )

    def _facility_facts_for_product(
        self, product: ProductInfo
    ) -> List[AllergenFact]:
        """Convert any facility profiles into allergen facts for this product."""
        facts: List[AllergenFact] = []
        for profile in list(product.facilities) + self.facility_profiles:
            facts.append(profile.to_fact())
        return facts

    @staticmethod
    def _aggregate_scores(scores: Iterable[float]) -> float:
        """
        Combine multiple scores using complementary probability to avoid
        over-counting when multiple signals point to the same allergen.
        """
        complement = 1.0
        for score in scores:
            complement *= max(0.0, 1.0 - min(score, 100.0) / 100.0)
        return min(100.0, (1.0 - complement) * 100.0)

    @staticmethod
    def _include_fact(fact: AllergenFact, user_profile: UserAllergyProfile) -> bool:
        """
        Respect user preferences about traces and facility cross-contact.
        """
        if fact.presence_type == PresenceType.FACILITY_RISK:
            return user_profile.avoid_facility_risk
        if fact.presence_type == PresenceType.MAY_CONTAIN:
            return user_profile.avoid_traces
        return True

    def _bhm_cross_contact_fact(self, product: ProductInfo, allergen_code: str) -> Optional[AllergenFact]:
        """
        Build an AllergenFact from the Bayesian cross-contact estimator.
        """
        payload = product.raw_payload or {}
        category_tags = payload.get("categories_tags") or []
        category = category_tags[0] if category_tags else payload.get("category") or ""
        brand = product.brand or payload.get("brands") or ""
        may_map = {}
        traces = product.traces_tags or payload.get("traces_tags") or []
        for tag in traces:
            if ":" in tag:
                _, key = tag.split(":", 1)
            else:
                key = tag
            may_map[key.upper()] = True

        product_features = {
            "id": product.ean,
            "category": category,
            "brand": brand,
            "ingredients": [],  # optional; not used in current estimator
            "may_contain": may_map,
            "category_stats": payload.get("category_stats", {}),
            "brand_stats": payload.get("brand_stats", {}),
            "allergens": [fact.allergen_code for fact in product.allergen_facts],
        }

        bhm = final_cross_contact_risk(product_features, allergen_code)
        risk_prob = bhm["risk"]
        if risk_prob <= 0:
            return None

        return AllergenFact(
            allergen_code=allergen_code,
            presence_type=PresenceType.FACILITY_RISK,
            source="bhm:cross_contact",
            weight=risk_prob,
            confidence=1.0,
        )
