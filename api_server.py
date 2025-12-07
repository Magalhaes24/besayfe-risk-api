"""
FastAPI wrapper for the allergen risk engine.

Endpoints:
- GET /health       : readiness probe
- POST /risk        : compute allergen risk for a given barcode + user allergens

Run locally:
    uvicorn api_server:app --host 0.0.0.0 --port 8000

Deployment tips (see comments at bottom) include Render, Railway, Fly.io, Codespaces.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator

from risk_engine import (
    FoodDatabase,
    OpenFoodFactsClient,
    RiskEngine,
    UserAllergyProfile,
)
from risk_engine.cross_contact_bhm import final_cross_contact_risk

app = FastAPI(
    title="Allergen Risk API",
    description="REST API for allergen risk scoring (OpenFoodFacts + Bayesian cross-contact).",
    version="1.0.0",
)

# CORS for broad consumption; tighten in production by setting allowed origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RiskRequest(BaseModel):
    barcode: str = Field(..., description="Product EAN/UPC barcode")
    user_allergens: List[str] = Field(..., description="List of allergen codes (e.g., MILK, PEANUT)")
    consider_may_contain: bool = Field(
        True,
        description="If true, treat 'may contain' / traces as risky (default: true).",
    )
    consider_facility: bool = Field(
        False,
        description="If true, include facility cross-contact in scoring (default: false).",
    )

    @validator("user_allergens")
    def _normalize_allergens(cls, v: List[str]) -> List[str]:
        return [code.strip().upper() for code in v if code.strip()]


class RiskResponse(BaseModel):
    product: Dict
    cross_contact: Dict
    risk: Dict


# Shared singletons
client = OpenFoodFactsClient()
food_db = FoodDatabase(preload=True)
engine = RiskEngine(product_source=client, food_database=food_db)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


def _product_dict(product) -> Dict:
    """Serialize ProductInfo into a JSON-friendly dict."""
    payload = product.raw_payload or {}
    return {
        "ean": product.ean,
        "name": product.name,
        "brand": product.brand,
        "source": product.source,
        "allergens_tags": payload.get("allergens_tags"),
        "traces_tags": payload.get("traces_tags"),
        "ingredients_text": payload.get("ingredients_text_en") or payload.get("ingredients_text"),
        "raw": payload,
    }


def _build_bhm_features(product) -> Dict:
    """Map ProductInfo/raw payload to the feature dict expected by the BHM helper."""
    payload = product.raw_payload or {}
    traces_tags = payload.get("traces_tags") or []
    may_contain = {}
    for tag in traces_tags:
        key = tag.split(":", 1)[-1].upper() if isinstance(tag, str) else str(tag).upper()
        may_contain[key] = True
    category_tags = payload.get("categories_tags") or []
    return {
        "id": product.ean,
        "category": category_tags[0] if category_tags else payload.get("category") or "",
        "brand": product.brand or payload.get("brands") or "",
        "ingredients": payload.get("ingredients") or [],
        "may_contain": may_contain,
        "category_stats": payload.get("category_stats", {}),
        "brand_stats": payload.get("brand_stats", {}),
        "allergens": [f.allergen_code for f in product.allergen_facts],
    }


def _compute_cross_contact(product, allergen_codes: List[str]) -> Dict[str, Dict[str, float]]:
    """Compute BHM cross-contact for each allergen."""
    features = _build_bhm_features(product)
    results: Dict[str, Dict[str, float]] = {}
    for code in allergen_codes:
        results[code] = final_cross_contact_risk(features, code)
    return results


@app.post("/risk", response_model=RiskResponse)
def risk(request: RiskRequest):
    # Fetch product
    product = client.get_product(request.barcode)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found on OpenFoodFacts")

    # Run engine
    profile = UserAllergyProfile(
        allergen_codes=request.user_allergens,
        avoid_traces=request.consider_may_contain,
        avoid_facility_risk=request.consider_facility,
    )
    result = engine.assess(ean=request.barcode, user_profile=profile)
    if not result:
        raise HTTPException(status_code=500, detail="Unable to compute risk for product")

    # Cross-contact per allergen
    cross_contact = (
        _compute_cross_contact(result.product, profile.normalized_codes())
        if request.consider_facility
        else {}
    )

    # Risk breakdown
    per_allergen = {
        code: {
            "score": detail.score,
            "reasons": detail.reasons,
        }
        for code, detail in result.per_allergen.items()
    }

    response = {
        "product": _product_dict(result.product),
        "cross_contact": cross_contact,
        "risk": {
            "per_allergen": per_allergen,
            "final_score": result.total_score,
        },
    }
    return response


if __name__ == "__main__":
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)

# Deployment quick notes:
# - Render.com: create a Web Service, point to repo, start command `uvicorn api_server:app --host 0.0.0.0 --port 8000`
# - Railway.app: create a service from repo, set same start command, expose 8000
# - Fly.io: use `fly launch` with Dockerfile; set internal port 8000 in fly.toml
# - GitHub Codespaces: run `uvicorn api_server:app --host 0.0.0.0 --port 8000` and forward port 8000
