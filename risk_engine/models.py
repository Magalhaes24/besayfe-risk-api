"""
Shared domain models used by the risk engine.

- PresenceType: classifies how an allergen is present (declared, trace, facility).
- AllergenFact: a single evidence item that an allergen may be present.
- FacilityAllergenProfile: reusable template for facility-driven facts.
- ProductInfo: normalized product representation independent of source.
- UserAllergyProfile: what the user wants to avoid and how strict to be.
- RiskDetail/RiskResult: scored output of the risk calculation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional


class PresenceType(str, Enum):
    CONTAINS = "contains"
    MAY_CONTAIN = "may_contain"
    FACILITY_RISK = "facility_risk"


@dataclass
class AllergenFact:
    """
    Represents a product-specific allergen fact aligned with the allergens table.
    """

    allergen_code: str
    presence_type: PresenceType
    source: str
    weight: float = 1.0  # proportion of product related to the allergen presence
    confidence: float = 1.0  # data quality confidence in [0, 1]

    def normalized_score(self) -> float:
        """
        Returns the 0-100 severity score for this allergen fact before user context.
        Facility risk uses the weight directly as a probability-style score to
        accommodate the Bayesian cross-contact model outputs.
        """
        if self.presence_type == PresenceType.FACILITY_RISK:
            prob = max(0.0, min(self.weight, 1.0))
            conf = max(0.0, min(self.confidence, 1.0))
            return min(100.0, 100.0 * prob * conf)

        base = {
            PresenceType.CONTAINS: 1.0,
            PresenceType.MAY_CONTAIN: 0.65,
            PresenceType.FACILITY_RISK: 0.45,
        }[self.presence_type]
        score = 100 * base * max(0.0, min(self.weight, 1.0)) * max(
            0.0, min(self.confidence, 1.0)
        )
        return min(100.0, score)


@dataclass
class FacilityAllergenProfile:
    """
    Mirrors facility_allergen_profile rows to allow facility-driven risk signals.
    """

    facility_id: Optional[int]
    allergen_code: str
    process_type: str
    proportion_of_products: Optional[float] = None

    def to_fact(self, source: str = "facility_profile") -> AllergenFact:
        weight = self.proportion_of_products if self.proportion_of_products else 0.5
        confidence = 0.6 if self.proportion_of_products is None else 0.8
        return AllergenFact(
            allergen_code=self.allergen_code,
            presence_type=PresenceType.FACILITY_RISK,
            source=source,
            weight=weight,
            confidence=confidence,
        )


@dataclass
class ProductInfo:
    """
    Standardized product model independent of the external provider.
    """

    ean: str
    name: str
    brand: Optional[str] = None
    manufacturer_id: Optional[int] = None
    source: str = "openfoodfacts"
    allergen_facts: List[AllergenFact] = field(default_factory=list)
    facilities: List[FacilityAllergenProfile] = field(default_factory=list)
    raw_payload: Optional[dict] = None
    traces_tags: Optional[list] = None

    def allergen_codes(self) -> Iterable[str]:
        return {fact.allergen_code for fact in self.allergen_facts}


@dataclass
class UserAllergyProfile:
    """
    Captures user preferences and allergies/intolerances.
    """

    allergen_codes: List[str]
    avoid_traces: bool = True
    avoid_facility_risk: bool = False

    def normalized_codes(self) -> List[str]:
        return [code.upper() for code in self.allergen_codes]


@dataclass
class RiskDetail:
    allergen_code: str
    score: float
    reasons: List[str] = field(default_factory=list)
    facts: List[AllergenFact] = field(default_factory=list)


@dataclass
class RiskResult:
    total_score: float
    product: ProductInfo
    per_allergen: Dict[str, RiskDetail]

    def worst_offender(self) -> Optional[RiskDetail]:
        if not self.per_allergen:
            return None
        return max(self.per_allergen.values(), key=lambda d: d.score)
