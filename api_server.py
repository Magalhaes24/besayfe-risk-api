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

import argparse
import json
from copy import deepcopy
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator

from risk_engine import (
    AllergySeverity,
    FoodDatabase,
    ImageTextProductSource,
    OpenFoodFactsClient,
    RiskEngine,
    UserAllergyProfile,
)
from risk_engine.cross_contact_bhm import final_cross_contact_risk
from main import append_history

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


class AllergenProfileRequest(BaseModel):
    profile_id: Optional[str] = Field(
        None, description="Client-provided identifier for this profile"
    )
    user_allergens: List[str] = Field(
        ..., description="List of allergen codes for this profile"
    )
    consider_may_contain: bool = Field(
        True,
        description="If true, treat 'may contain' / traces as risky for this profile.",
    )
    consider_facility: bool = Field(
        False,
        description="If true, include facility cross-contact in scoring for this profile.",
    )
    allergen_severities: Optional[Dict[str, str]] = Field(
        None,
        description=(
            "Severity per allergen code: LOW / MEDIUM / HIGH. "
            "Allergens not listed default to MEDIUM."
        ),
    )

    @validator("user_allergens")
    def _normalize_allergens(cls, v: List[str]) -> List[str]:
        normalized = [code.strip().upper() for code in v if code.strip()]
        if not normalized:
            raise ValueError("user_allergens must include at least one code")
        return normalized

    @validator("allergen_severities")
    def _normalize_severities(
        cls, v: Optional[Dict[str, str]]
    ) -> Optional[Dict[str, str]]:
        if v is None:
            return v
        valid = {"LOW", "MEDIUM", "HIGH"}
        result: Dict[str, str] = {}
        for code, sev in v.items():
            sev_upper = sev.strip().upper()
            if sev_upper not in valid:
                raise ValueError(
                    f"Invalid severity '{sev}' for '{code}'. Must be LOW, MEDIUM, or HIGH."
                )
            result[code.strip().upper()] = sev_upper
        return result


class RiskRequest(BaseModel):
    barcode: str = Field(..., description="Product EAN/UPC barcode")
    user_allergens: Optional[List[str]] = Field(
        None,
        description="Legacy single profile list of allergen codes (e.g., MILK, PEANUT)",
    )
    consider_may_contain: bool = Field(
        True,
        description="If true, treat 'may contain' / traces as risky (default: true).",
    )
    consider_facility: bool = Field(
        False,
        description="If true, include facility cross-contact in scoring (default: false).",
    )
    allergen_profiles: Optional[List[AllergenProfileRequest]] = Field(
        None,
        description="One or more allergen profiles. If provided, overrides legacy single-profile fields.",
    )

    @validator("user_allergens")
    def _normalize_allergens(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
        return [code.strip().upper() for code in v if code.strip()]


class RiskResponse(BaseModel):
    product: Dict
    cross_contact: Dict
    risk: Dict
    summary: Optional[Dict] = None
    participant_scores: Optional[List[Dict]] = None
    computed_overall_risk: Optional[float] = None
    displayed_overall_risk: Optional[float] = None


# Shared singletons
client = OpenFoodFactsClient()
food_db = FoodDatabase(preload=True)
engine = RiskEngine(product_source=client, food_database=food_db)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> Dict[str, str]:
    """Basic landing route to avoid 404s on the root path."""
    return {"status": "ok", "service": "allergen-risk-api"}


@app.get("/favicon.ico")
def favicon() -> Dict[str, str]:
    """Placeholder favicon route to avoid browser 404 noise."""
    return {"status": "ok"}


def _product_dict(product, include_raw: bool = True) -> Dict:
    """Serialize ProductInfo into a JSON-friendly dict."""
    payload = product.raw_payload or {}
    data = {
        "ean": product.ean,
        "name": product.name,
        "brand": product.brand,
        "source": product.source,
        "data_notes": getattr(product, "data_notes", []),
        "allergens_tags": payload.get("allergens_tags"),
        "traces_tags": payload.get("traces_tags"),
        "ingredients_text": payload.get("ingredients_text_en") or payload.get("ingredients_text"),
    }
    if include_raw:
        data["raw"] = payload
    return data


def _summary_dict(result) -> Dict:
    """Create a compact, human-friendly summary for the response."""
    return {
        "product": f"{result.product.name} ({result.product.ean})",
        "total_score": result.total_score,
        "allergens_found": [
            f"{fact.allergen_code}:{fact.presence_type.value}"
            for fact in result.product.allergen_facts
        ],
        "ingredients_text": (result.product.raw_payload or {}).get("ingredients_text")
        or (result.product.raw_payload or {}).get("ingredients_text_en")
        or "",
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


def _aggregate_scores(scores: List[float]) -> float:
    """Combine 0-100 scores using complementary probability."""
    complement = 1.0
    for score in scores:
        complement *= max(0.0, 1.0 - min(score, 100.0) / 100.0)
    return min(100.0, (1.0 - complement) * 100.0)


def _profile_output(result, profile: AllergenProfileRequest) -> Dict:
    per_allergen = {
        code: {
            "score": detail.score,
            "severity": detail.applied_severity.value,
            "reasons": detail.reasons,
        }
        for code, detail in result.per_allergen.items()
    }
    cross_contact = (
        _compute_cross_contact(result.product, profile.user_allergens)
        if profile.consider_facility
        else {}
    )
    return {
        "profile_id": profile.profile_id,
        "user_allergens": profile.user_allergens,
        "consider_may_contain": profile.consider_may_contain,
        "consider_facility": profile.consider_facility,
        "cross_contact": cross_contact,
        "risk": {
            "per_allergen": per_allergen,
            "final_score": result.total_score,
        },
    }


@app.post("/risk", response_model=RiskResponse)
def risk(request: RiskRequest, include_raw: bool = True):
    if (not request.allergen_profiles) and (not request.user_allergens):
        raise HTTPException(
            status_code=400,
            detail="Provide either allergen_profiles (one or more) or user_allergens (legacy single profile)",
        )

    # Fetch product
    product = client.get_product(request.barcode)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found on OpenFoodFacts")

    # Resolve into one or more effective profiles.
    if request.allergen_profiles:
        effective_profiles = request.allergen_profiles
    else:
        effective_profiles = [
            AllergenProfileRequest(
                profile_id="default",
                user_allergens=request.user_allergens or [],
                consider_may_contain=request.consider_may_contain,
                consider_facility=request.consider_facility,
            )
        ]

    profile_results: List[Dict] = []
    profile_scores: List[float] = []
    combined_allergen_scores: Dict[str, List[float]] = {}
    combined_allergen_severities: Dict[str, List[str]] = {}
    first_result = None

    for idx, p in enumerate(effective_profiles):
        severities = {
            code: AllergySeverity(sev.lower())
            for code, sev in (p.allergen_severities or {}).items()
        }
        profile = UserAllergyProfile(
            allergen_codes=p.user_allergens,
            avoid_traces=p.consider_may_contain,
            avoid_facility_risk=p.consider_facility,
            allergen_severities=severities,
        )
        # RiskEngine mutates product facts during assessment; use a fresh copy per profile.
        result = engine.assess_product(product=deepcopy(product), user_profile=profile)
        if not result:
            raise HTTPException(status_code=500, detail="Unable to compute risk for product")
        if idx == 0:
            first_result = result

        profile_results.append(_profile_output(result, p))
        profile_scores.append(result.total_score)
        for code, detail in result.per_allergen.items():
            combined_allergen_scores.setdefault(code, []).append(detail.score)
            combined_allergen_severities.setdefault(code, []).append(
                detail.applied_severity.value
            )

    if not first_result:
        raise HTTPException(status_code=500, detail="Unable to compute risk for product")

    combined_per_allergen = {
        code: {
            "score": round(_aggregate_scores(scores), 2),
            "profile_count": len(scores),
            "severities": combined_allergen_severities.get(code, []),
        }
        for code, scores in combined_allergen_scores.items()
    }
    combined_final_score = round(_aggregate_scores(profile_scores), 2)
    participant_scores = [
        {
            "profile_id": p.get("profile_id"),
            "final_score": p.get("risk", {}).get("final_score"),
            "allergens": p.get("user_allergens", []),
            "per_allergen": p.get("risk", {}).get("per_allergen", {}),
        }
        for p in profile_results
    ]
    combined_cross_contact: Dict[str, Dict[str, float]] = {}
    for p in profile_results:
        for code, value in p["cross_contact"].items():
            combined_cross_contact[code] = value

    summary = _summary_dict(first_result)
    summary["total_score"] = combined_final_score

    response = {
        "product": _product_dict(first_result.product, include_raw=include_raw),
        "cross_contact": combined_cross_contact,
        "risk": {
            "per_allergen": combined_per_allergen,
            "final_score": combined_final_score,
            "combined": {
                "per_allergen": combined_per_allergen,
                "final_score": combined_final_score,
            },
            "profiles": profile_results,
            "participant_scores": participant_scores,
            "computed_overall_risk": combined_final_score,
            "displayed_overall_risk": combined_final_score,
        },
        "summary": summary,
        "participant_scores": participant_scores,
        "computed_overall_risk": combined_final_score,
        "displayed_overall_risk": combined_final_score,
    }

    # Best-effort audit log to the shared history.csv with API source marker.
    try:
        history_result = deepcopy(first_result)
        history_result.total_score = combined_final_score
        history_result.per_allergen = {
            code: deepcopy(first_result.per_allergen.get(code))
            for code in first_result.per_allergen
        }
        append_history(
            argparse.Namespace(
                ean=request.barcode,
                allergies=sorted(
                    {
                        code
                        for p in effective_profiles
                        for code in p.user_allergens
                    }
                ),
            ),
            history_result,
            lang="en",
            command_label="api_risk",
            request_source="api",
        )
    except Exception:
        # Logging failures should not break the API response
        pass
    return response


@app.post("/risk/image", response_model=RiskResponse)
def risk_from_image(
    file: UploadFile = File(..., description="Image of label/menu/technical sheet"),
    user_allergens: str = Form(..., description="Comma-separated allergen codes"),
    consider_may_contain: bool = Form(
        True, description="If true, treat 'may contain' / traces as risky."
    ),
    consider_facility: bool = Form(
        False, description="If true, include facility cross-contact in scoring."
    ),
    ocr_lang: str = Form("eng", description="Tesseract language code (default: eng)"),
    tesseract_cmd: Optional[str] = Form(
        None, description="Optional path to tesseract.exe if not on PATH"
    ),
    reference_id: Optional[str] = Form(
        None, description="Optional identifier for the image input"
    ),
    include_raw: bool = Form(
        False, description="Include raw OCR payload in the response (default: false)"
    ),
    allergen_severities: Optional[str] = Form(
        None,
        description=(
            "Optional JSON object mapping allergen codes to severity levels "
            "(LOW/MEDIUM/HIGH), e.g. '{\"MILK\":\"HIGH\",\"PEANUT\":\"LOW\"}'. "
            "Allergens not listed default to MEDIUM."
        ),
    ),
):
    codes = [code.strip().upper() for code in user_allergens.split(",") if code.strip()]
    if not codes:
        raise HTTPException(status_code=400, detail="user_allergens must include at least one code")

    image_bytes = file.file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Uploaded image is empty")

    # Parse optional severity map from JSON string.
    severities: Dict[str, AllergySeverity] = {}
    if allergen_severities:
        try:
            raw_sev = json.loads(allergen_severities)
        except Exception:
            raise HTTPException(
                status_code=400, detail="allergen_severities must be a valid JSON object"
            )
        valid = {"LOW", "MEDIUM", "HIGH"}
        for code, sev in raw_sev.items():
            sev_upper = str(sev).strip().upper()
            if sev_upper not in valid:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid severity '{sev}' for '{code}'. Must be LOW, MEDIUM, or HIGH.",
                )
            severities[code.strip().upper()] = AllergySeverity(sev_upper.lower())

    ocr_source = ImageTextProductSource(
        lang=ocr_lang or "eng", tesseract_cmd=tesseract_cmd
    )
    try:
        product = ocr_source.product_from_image(
            image_bytes=image_bytes,
            reference_id=reference_id or file.filename,
            name=file.filename or "Image input",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"OCR failed: {exc}") from exc

    profile = UserAllergyProfile(
        allergen_codes=codes,
        avoid_traces=consider_may_contain,
        avoid_facility_risk=consider_facility,
        allergen_severities=severities,
    )
    result = engine.assess_product(product, user_profile=profile)
    if not result:
        raise HTTPException(status_code=500, detail="Unable to compute risk for OCR input")

    cross_contact = (
        _compute_cross_contact(result.product, profile.normalized_codes())
        if consider_facility
        else {}
    )
    per_allergen = {
        code: {
            "score": detail.score,
            "reasons": detail.reasons,
        }
        for code, detail in result.per_allergen.items()
    }
    response = {
        "product": _product_dict(result.product, include_raw=include_raw),
        "cross_contact": cross_contact,
        "risk": {
            "per_allergen": per_allergen,
            "final_score": result.total_score,
            "participant_scores": [
                {
                    "profile_id": "default",
                    "final_score": result.total_score,
                    "allergens": codes,
                    "per_allergen": per_allergen,
                }
            ],
            "computed_overall_risk": result.total_score,
            "displayed_overall_risk": result.total_score,
        },
        "summary": _summary_dict(result),
        "participant_scores": [
            {
                "profile_id": "default",
                "final_score": result.total_score,
                "allergens": codes,
                "per_allergen": per_allergen,
            }
        ],
        "computed_overall_risk": result.total_score,
        "displayed_overall_risk": result.total_score,
    }
    return response


if __name__ == "__main__":
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)

# Deployment quick notes:
# - Render.com: create a Web Service, point to repo, start command `uvicorn api_server:app --host 0.0.0.0 --port 8000`
# - Railway.app: create a service from repo, set same start command, expose 8000
# - Fly.io: use `fly launch` with Dockerfile; set internal port 8000 in fly.toml
# - GitHub Codespaces: run `uvicorn api_server:app --host 0.0.0.0 --port 8000` and forward port 8000
