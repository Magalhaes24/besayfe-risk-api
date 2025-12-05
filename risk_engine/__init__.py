"""
Risk engine package for assessing allergen exposure risk for a given product EAN
and user allergy profile.

Expose the main classes so consumers can import directly from the package.
"""

from .models import (
    AllergenFact,
    FacilityAllergenProfile,
    PresenceType,
    ProductInfo,
    RiskDetail,
    RiskResult,
    UserAllergyProfile,
)
from .db_repository import DatabaseProductSource
from .food_db import FoodDatabase
from .openfoodfacts_client import OpenFoodFactsClient
from .risk_engine import RiskEngine
from .allergens import allergen_label, resolve_allergen_code

__all__ = [
    "AllergenFact",
    "DatabaseProductSource",
    "FacilityAllergenProfile",
    "PresenceType",
    "ProductInfo",
    "RiskDetail",
    "RiskResult",
    "UserAllergyProfile",
    "FoodDatabase",
    "OpenFoodFactsClient",
    "RiskEngine",
    "allergen_label",
    "resolve_allergen_code",
]
