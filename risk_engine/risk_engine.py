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

# Standard library typing helpers.
from typing import Dict, Iterable, List, Optional

# Domain models and data sources used to compute risk.
from .models import (
    AllergenFact,
    AllergySeverity,
    FacilityAllergenProfile,
    PresenceType,
    ProductInfo,
    RiskDetail,
    RiskResult,
    UserAllergyProfile,
)
# Product source contract and optional enrichment helpers.
from .openfoodfacts_client import ProductDataSource
from .food_db import FoodDatabase
from .cross_contact_bhm import final_cross_contact_risk


class RiskEngine:
    """
    Orchestrates fetching data, standardizing it, and computing a 0-100 risk score.
    Inject different data sources or facility profiles to adapt to your stack.
    """

    # Multipliers applied to the per-allergen score based on allergy severity.
    SEVERITY_MULTIPLIERS = {
        AllergySeverity.LOW: 0.5,
        AllergySeverity.MEDIUM: 1.0,
        AllergySeverity.HIGH: 1.5,
    }

    # Proximity-based contamination boosts: if a triggering allergen is present,
    # slightly raise risk for a closely related allergen (e.g., nuts -> peanut).
    PROXIMITY_TRIGGERS = {
        # Peanut is handled like a nut category for risk signaling.
        "PEANUT": [
            (
                "TREE_NUTS",
                1.0,
                1.0,
                "Peanuts are a nut family and indicate nut exposure",
                PresenceType.CONTAINS,
            )
        ],
        # The reverse: tree nut handling can contaminate peanut lines.
        "TREE_NUTS": [
            (
                "PEANUT",
                0.8,
                0.85,
                "Close contact with peanuts",
                PresenceType.MAY_CONTAIN,
            )
        ],
    }

    def __init__(
        self,
        product_source: ProductDataSource,
        facility_profiles: Optional[Iterable[FacilityAllergenProfile]] = None,
        fallback_score: float = 5.0,
        food_database: Optional[FoodDatabase] = None,
    ):
        # Capture dependencies and defaults for later assessments.
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
        # Delegate to the injected data source.
        product = self.product_source.get_product(ean)
        if not product:
            return None
        # Run the full assessment pipeline on the fetched product.
        return self._assess_product(product, user_profile)

    def assess_product(
        self, product: ProductInfo, user_profile: UserAllergyProfile
    ) -> Optional[RiskResult]:
        """
        Assess a fully formed ProductInfo (e.g., OCR inputs) without fetching by EAN.
        """
        # Guard against missing product input.
        if not product:
            return None
        return self._assess_product(product, user_profile)

    def _assess_product(
        self, product: ProductInfo, user_profile: UserAllergyProfile
    ) -> Optional[RiskResult]:

        # Attach facility-based facts if we have them.
        facility_facts = self._facility_facts_for_product(product)
        product.allergen_facts.extend(facility_facts)
        # Optionally enrich with FoodDB-derived allergen facts.
        if self.food_database:
            enriched_facts = self.food_database.infer_allergen_facts(
                product, user_profile.normalized_codes()
            )
            product.allergen_facts.extend(enriched_facts)

        # Initialize the per-allergen results bucket and normalize codes once.
        per_allergen: Dict[str, RiskDetail] = {}
        normalized_codes = user_profile.normalized_codes()

        # Compute a score for each allergen the user cares about.
        for code in normalized_codes:
            # Filter product facts down to the current allergen and user preferences.
            facts = [
                fact
                for fact in product.allergen_facts
                if fact.allergen_code == code
                and self._include_fact(fact, user_profile)
            ]
            # Add proximity-based cross-contact facts.
            facts.extend(
                self._proximity_facts(product, code, user_profile=user_profile)
            )

            # Inject Bayesian facility cross-contact fact if requested.
            if user_profile.avoid_facility_risk:
                bhm_fact = self._bhm_cross_contact_fact(product, code)
                if bhm_fact:
                    facts.append(bhm_fact)

            # Aggregate the facts or fall back to a conservative default.
            if facts:
                scores = [fact.normalized_score() for fact in facts]
                score = self._aggregate_scores(scores)
                reasons = [self._format_reason(fact) for fact in facts]
            else:
                score = self.fallback_score
                reasons = self._fallback_reasons(product)

            # Scale score by the user's severity for this allergen.
            severity = user_profile.severity_for(code)
            score = min(100.0, score * self.SEVERITY_MULTIPLIERS[severity])
            if severity != AllergySeverity.MEDIUM:
                multiplier = self.SEVERITY_MULTIPLIERS[severity]
                direction = "increases" if multiplier > 1.0 else "reduces"
                pct = abs(round((multiplier - 1.0) * 100))
                reasons.append(
                    f"Your {severity.value.upper()} sensitivity {direction} the score by {pct}%"
                )

            # Store the per-allergen breakdown for callers.
            per_allergen[code] = RiskDetail(
                allergen_code=code, score=score, reasons=reasons, facts=facts,
                applied_severity=severity,
            )

        # Aggregate per-allergen scores into the overall risk score.
        total_score = self._aggregate_scores([d.score for d in per_allergen.values()])

        # Return the completed risk result payload.
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
        # Merge facility profiles from the product and the engine configuration.
        for profile in list(product.facilities) + self.facility_profiles:
            facts.append(profile.to_fact())
        return facts

    @staticmethod
    def _fallback_reasons(product: ProductInfo) -> List[str]:
        """
        Provide more context on why the fallback score is being applied.
        """
        # Prefer any upstream data notes for context.
        notes = list(getattr(product, "data_notes", []) or [])
        if not notes:
            return ["No direct data; applying conservative fallback"]
        # Include a generic fallback suffix so callers know the score is conservative.
        notes.append("Applying conservative fallback score")
        return notes

    @staticmethod
    def _format_reason(fact: AllergenFact) -> str:
        """
        Convert an AllergenFact into a human-readable explanation for callers.
        """
        src = fact.source or ""
        ptype = fact.presence_type

        # Map source prefixes to readable channel names.
        if src.startswith("proximity:"):
            related = src.split(":", 1)[1].replace("_", " ").title()
            if ptype == PresenceType.CONTAINS:
                return (
                    f"Cross-family risk: {related} is present and shares an allergen "
                    f"family with this allergen (confidence {fact.confidence:.0%})"
                )
            return (
                f"Shared-line risk: {related} is handled on the same processing line "
                f"(confidence {fact.confidence:.0%})"
            )

        if src.startswith("bhm:"):
            pct = round(fact.weight * 100)
            return (
                f"Bayesian facility model: estimates {pct}% probability of "
                f"cross-contact based on product category and brand history"
            )

        if src.startswith("facility_profile"):
            pct = round(fact.weight * 100)
            return (
                f"Facility record: this manufacturing site handles this allergen "
                f"in {pct}% of its products"
            )

        if "allergens_tags" in src or "allergens" in src:
            return "Declared allergen: officially listed on the product's allergen label"

        if "traces_tags" in src or "traces" in src:
            return "Manufacturer warning: product label states it may contain this allergen"

        if "ingredients_text" in src and "ocr" in src:
            return "Detected in ingredient text extracted from the product image (OCR)"

        if "ingredients_text" in src:
            return "Detected in the product's ingredient list text"

        if "fooddb" in src or "inference" in src:
            return (
                f"Ingredient inference: FoodDB database identified this allergen in "
                f"a known ingredient (confidence {fact.confidence:.0%})"
            )

        if "db:" in src:
            return "Found in product allergen database record"

        # Generic fallback: still more informative than the raw internal string.
        channel = src.replace("_", " ").replace(":", " → ")
        ptype_label = {
            PresenceType.CONTAINS: "Directly present",
            PresenceType.MAY_CONTAIN: "Possible trace",
            PresenceType.FACILITY_RISK: "Facility risk",
        }.get(ptype, ptype.value)
        return f"{ptype_label} via {channel} (confidence {fact.confidence:.0%})"

    @staticmethod
    def _aggregate_scores(scores: Iterable[float]) -> float:
        """
        Combine multiple scores using complementary probability to avoid
        over-counting when multiple signals point to the same allergen.
        """
        # Multiply complements to combine independent risk signals.
        complement = 1.0
        for score in scores:
            complement *= max(0.0, 1.0 - min(score, 100.0) / 100.0)
        return min(100.0, (1.0 - complement) * 100.0)

    @staticmethod
    def _include_fact(fact: AllergenFact, user_profile: UserAllergyProfile) -> bool:
        """
        Respect user preferences about traces and facility cross-contact.
        """
        # Filter out fact types the user has opted to ignore.
        if fact.presence_type == PresenceType.FACILITY_RISK:
            return user_profile.avoid_facility_risk
        if fact.presence_type == PresenceType.MAY_CONTAIN:
            return user_profile.avoid_traces
        return True

    def _proximity_facts(
        self, product: ProductInfo, target_code: str, user_profile: UserAllergyProfile
    ) -> List[AllergenFact]:
        """
        Add may_contain facts when closely related allergens are present,
        acknowledging higher contamination probability on shared lines.
        """
        # Look up related allergens that should raise proximity risk.
        triggers = self.PROXIMITY_TRIGGERS.get(target_code.upper(), [])
        if not triggers:
            return []

        # Only emit proximity facts if the triggering allergen is already present.
        existing_codes = {fact.allergen_code for fact in product.allergen_facts}
        facts: List[AllergenFact] = []
        for trigger_code, weight, confidence, rationale, presence_type in triggers:
            if trigger_code not in existing_codes:
                continue
            fact = AllergenFact(
                allergen_code=target_code.upper(),
                presence_type=presence_type,
                source=f"proximity:{trigger_code.lower()}",
                weight=weight,
                confidence=confidence,
            )
            if self._include_fact(fact, user_profile):
                facts.append(fact)
        return facts

    def _bhm_cross_contact_fact(self, product: ProductInfo, allergen_code: str) -> Optional[AllergenFact]:
        """
        Build an AllergenFact from the Bayesian cross-contact estimator.
        """
        # Extract features from the product payload that the estimator expects.
        payload = product.raw_payload or {}
        category_tags = payload.get("categories_tags") or []
        category = category_tags[0] if category_tags else payload.get("category") or ""
        brand = product.brand or payload.get("brands") or ""
        may_map = {}
        traces = product.traces_tags or payload.get("traces_tags") or []
        # Normalize traces tags into a may_contain lookup map.
        for tag in traces:
            if ":" in tag:
                _, key = tag.split(":", 1)
            else:
                key = tag
            may_map[key.upper()] = True

        # Build the estimator input payload with model-ready fields.
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

        # Run the estimator and map its risk to a facility-risk AllergenFact.
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
