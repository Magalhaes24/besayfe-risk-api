"""
Risk engine package for assessing allergen exposure risk for a given product EAN
and user allergy profile.

Expose the main classes so consumers can import directly from the package.
"""

# Re-export core models for convenient top-level imports.
from .models import (
    AllergenFact,
    FacilityAllergenProfile,
    PresenceType,
    ProductInfo,
    RiskDetail,
    RiskResult,
    UserAllergyProfile,
)
# Re-export data sources and engine utilities for simple package use.
from .db_repository import DatabaseProductSource
from .food_db import FoodDatabase
from .openfoodfacts_client import OpenFoodFactsClient
from .image_ocr import ImageTextProductSource
from .risk_engine import RiskEngine
# Re-export allergen helpers that callers commonly need.
from .allergens import (
    allergen_label,
    detect_allergens_in_ingredient_texts,
    resolve_allergen_code,
)

# Keep __all__ in sync with the public symbols above.
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
    "ImageTextProductSource",
    "RiskEngine",
    "allergen_label",
    "detect_allergens_in_ingredient_texts",
    "resolve_allergen_code",
]
