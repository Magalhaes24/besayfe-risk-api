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
import logging
import os
import re
import requests
from copy import deepcopy
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from risk_engine import (
    AllergySeverity,
    FoodDatabase,
    ImageTextProductSource,
    OpenFoodFactsClient,
    RiskEngine,
    UserAllergyProfile,
)
from risk_engine.cross_contact_bhm import final_cross_contact_risk
from risk_engine.allergen_labels import allergen_label
from main import append_history

app = FastAPI(
    title="Allergen Risk API",
    description="REST API for allergen risk scoring (OpenFoodFacts + Bayesian cross-contact).",
    version="1.0.0",
)

# CORS: set ALLOWED_ORIGINS env var to a comma-separated list of origins for production.
# Example: ALLOWED_ORIGINS=https://app.example.com,https://admin.example.com
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
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

    @field_validator("user_allergens")
    @classmethod
    def _normalize_allergens(cls, v: List[str]) -> List[str]:
        normalized = [code.strip().upper() for code in v if code.strip()]
        if not normalized:
            raise ValueError("user_allergens must include at least one code")
        return normalized

    @field_validator("allergen_severities")
    @classmethod
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

    @field_validator("user_allergens")
    @classmethod
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

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_RISK_LEVELS = [
    (85.0, "CRITICAL", "This product is very likely unsafe. Avoid entirely."),
    (60.0, "HIGH",     "Strong evidence this product contains or may contain this allergen. Avoid."),
    (30.0, "MODERATE", "Some risk signals detected. Consult ingredients carefully before consuming."),
    (10.0, "LOW",      "Minimal signals detected. Low but non-zero risk."),
    (0.0,  "SAFE",     "No significant allergen signals found for this item."),
]


def _risk_level(score: float) -> Dict[str, str]:
    """Return a risk tier label and guidance message for a 0-100 score."""
    for threshold, label, guidance in _RISK_LEVELS:
        if score >= threshold:
            return {"label": label, "guidance": guidance}
    return {"label": "SAFE", "guidance": _RISK_LEVELS[-1][2]}


def _evidence_list(facts) -> List[Dict]:
    """Serialize AllergenFact objects into structured evidence entries."""
    out = []
    for fact in facts:
        ptype = fact.presence_type.value if hasattr(fact.presence_type, "value") else str(fact.presence_type)
        out.append({
            "type": ptype,
            "source": fact.source,
            "weight": round(fact.weight, 3),
            "confidence": round(fact.confidence, 3),
            "raw_score": round(fact.normalized_score(), 2),
        })
    return out


def _cross_contact_annotated(code: str, cc: Dict) -> Dict:
    """Annotate a raw BHM cross-contact dict with human-readable fields."""
    risk_pct = round(cc.get("risk", 0.0) * 100, 1)
    prob_pct = round(cc.get("probability", 0.0) * 100, 1)
    lower_pct = round(cc.get("lower_ci", 0.0) * 100, 1)
    upper_pct = round(cc.get("upper_ci", 0.0) * 100, 1)
    signal = round(cc.get("signal", 0.0), 3)

    parts = []
    if cc.get("presence", 0.0) > 0:
        parts.append("allergen is declared present")
    if cc.get("may_contain", 0.0) > 0:
        parts.append("'may contain' label is present")
    if signal > 0.05:
        parts.append(f"ingredient co-occurrence signal of {signal:.2f}")
    if not parts:
        parts.append("no direct label evidence — estimate based on category/brand priors only")

    explanation = (
        f"Bayesian cross-contact model estimates {risk_pct}% combined risk for {code}. "
        f"Contributing factors: {'; '.join(parts)}. "
        f"Posterior probability: {prob_pct}% (95% CI: {lower_pct}%–{upper_pct}%)."
    )
    return {
        **cc,
        "risk_level": _risk_level(cc.get("risk", 0.0) * 100)["label"],
        "risk_percent": risk_pct,
        "probability_percent": prob_pct,
        "confidence_interval": f"{lower_pct}%–{upper_pct}%",
        "explanation": explanation,
    }


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


def _summary_dict(result, combined_score: Optional[float] = None) -> Dict:
    """Create a compact, human-friendly summary for the response."""
    score = combined_score if combined_score is not None else result.total_score
    level = _risk_level(score)

    worst = result.worst_offender() if hasattr(result, "worst_offender") else None
    worst_code = worst.allergen_code if worst else None
    worst_name = allergen_label(worst_code) if worst_code else None

    # Deduplicate allergen signals into a readable list.
    seen: set = set()
    allergens_found = []
    for fact in result.product.allergen_facts:
        key = f"{fact.allergen_code}:{fact.presence_type.value}"
        if key not in seen:
            seen.add(key)
            label = allergen_label(fact.allergen_code)
            allergens_found.append({
                "code": fact.allergen_code,
                "display_name": label,
                "presence_type": fact.presence_type.value,
            })

    if level["label"] in ("CRITICAL", "HIGH"):
        recommendation = (
            f"Do not consume this product. "
            + (f"{worst_name or worst_code} was identified as the highest risk allergen. " if worst_code else "")
            + "Consult a medical professional if accidental exposure occurs."
        )
    elif level["label"] == "MODERATE":
        recommendation = (
            "Exercise caution. Review the full ingredient list and allergen declarations "
            "before consuming. When in doubt, avoid the product."
        )
    elif level["label"] == "LOW":
        recommendation = (
            "Low risk detected, but no product is completely free of trace risk. "
            "Check the label for your specific allergen before consuming."
        )
    else:
        recommendation = "No significant allergen signals found. Always verify the ingredient label."

    data_notes = getattr(result.product, "data_notes", []) or []
    if not result.product.allergen_facts:
        data_quality = "limited"
    elif data_notes:
        data_quality = "partial"
    else:
        data_quality = "complete"

    return {
        "product": f"{result.product.name} ({result.product.ean})",
        "total_score": score,
        "risk_level": level["label"],
        "guidance": level["guidance"],
        "recommendation": recommendation,
        "highest_risk_allergen": worst_name or worst_code,
        "safe_to_consume": level["label"] in ("SAFE", "LOW"),
        "allergens_found": allergens_found,
        "data_quality": data_quality,
        "data_notes": data_notes,
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


def _compute_cross_contact(product, allergen_codes: List[str]) -> Dict[str, Dict]:
    """Compute BHM cross-contact for each allergen and annotate with human-readable fields."""
    features = _build_bhm_features(product)
    results: Dict[str, Dict] = {}
    for code in allergen_codes:
        raw = final_cross_contact_risk(features, code)
        results[code] = _cross_contact_annotated(code, raw)
    return results


def _aggregate_scores(scores: List[float]) -> float:
    """Combine 0-100 scores using complementary probability."""
    complement = 1.0
    for score in scores:
        complement *= max(0.0, 1.0 - min(score, 100.0) / 100.0)
    return min(100.0, (1.0 - complement) * 100.0)


def _profile_output(result, profile: AllergenProfileRequest) -> Dict:
    per_allergen = {}
    for code, detail in result.per_allergen.items():
        level = _risk_level(detail.score)
        per_allergen[code] = {
            "display_name": allergen_label(code),
            "score": round(detail.score, 2),
            "risk_level": level["label"],
            "guidance": level["guidance"],
            "severity": detail.applied_severity.value,
            "reasons": detail.reasons,
            "evidence": _evidence_list(detail.facts),
        }
    cross_contact = (
        _compute_cross_contact(result.product, profile.user_allergens)
        if profile.consider_facility
        else {}
    )
    profile_level = _risk_level(result.total_score)
    return {
        "profile_id": profile.profile_id,
        "user_allergens": profile.user_allergens,
        "consider_may_contain": profile.consider_may_contain,
        "consider_facility": profile.consider_facility,
        "cross_contact": cross_contact,
        "risk": {
            "per_allergen": per_allergen,
            "final_score": round(result.total_score, 2),
            "risk_level": profile_level["label"],
            "guidance": profile_level["guidance"],
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

    combined_per_allergen = {}
    for code, scores in combined_allergen_scores.items():
        agg = round(_aggregate_scores(scores), 2)
        level = _risk_level(agg)
        combined_per_allergen[code] = {
            "display_name": allergen_label(code),
            "score": agg,
            "risk_level": level["label"],
            "guidance": level["guidance"],
            "profile_count": len(scores),
            "severities": combined_allergen_severities.get(code, []),
        }

    combined_final_score = round(_aggregate_scores(profile_scores), 2)
    combined_level = _risk_level(combined_final_score)
    participant_scores = [
        {
            "profile_id": p.get("profile_id"),
            "final_score": p.get("risk", {}).get("final_score"),
            "risk_level": p.get("risk", {}).get("risk_level"),
            "allergens": p.get("user_allergens", []),
            "per_allergen": p.get("risk", {}).get("per_allergen", {}),
        }
        for p in profile_results
    ]
    combined_cross_contact: Dict[str, Dict] = {}
    for p in profile_results:
        for code, value in p["cross_contact"].items():
            combined_cross_contact[code] = value

    summary = _summary_dict(first_result, combined_score=combined_final_score)

    response = {
        "product": _product_dict(first_result.product, include_raw=include_raw),
        "cross_contact": combined_cross_contact,
        "risk": {
            "per_allergen": combined_per_allergen,
            "final_score": combined_final_score,
            "risk_level": combined_level["label"],
            "guidance": combined_level["guidance"],
            "combined": {
                "per_allergen": combined_per_allergen,
                "final_score": combined_final_score,
                "risk_level": combined_level["label"],
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
        logger.warning("Failed to write audit history", exc_info=True)
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

    # Reject unsafe tesseract_cmd values — only bare executable names are allowed.
    if tesseract_cmd is not None and not re.fullmatch(r"[A-Za-z0-9_\-\.]+", tesseract_cmd):
        raise HTTPException(
            status_code=400,
            detail="tesseract_cmd must be a plain executable name (no path separators or special characters)",
        )

    _MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
    image_bytes = file.file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Uploaded image is empty")
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds 10 MB size limit")

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
    except requests.Timeout:
        raise HTTPException(status_code=504, detail="OCR service timed out")
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"OCR service unavailable: {exc}")
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Internal OCR processing error")

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
    final_score = round(result.total_score, 2)
    final_level = _risk_level(final_score)
    per_allergen = {}
    for code, detail in result.per_allergen.items():
        lvl = _risk_level(detail.score)
        per_allergen[code] = {
            "display_name": allergen_label(code),
            "score": round(detail.score, 2),
            "risk_level": lvl["label"],
            "guidance": lvl["guidance"],
            "severity": detail.applied_severity.value,
            "reasons": detail.reasons,
            "evidence": _evidence_list(detail.facts),
        }
    participant_entry = {
        "profile_id": "default",
        "final_score": final_score,
        "risk_level": final_level["label"],
        "allergens": codes,
        "per_allergen": per_allergen,
    }
    response = {
        "product": _product_dict(result.product, include_raw=include_raw),
        "cross_contact": cross_contact,
        "risk": {
            "per_allergen": per_allergen,
            "final_score": final_score,
            "risk_level": final_level["label"],
            "guidance": final_level["guidance"],
            "participant_scores": [participant_entry],
            "computed_overall_risk": final_score,
            "displayed_overall_risk": final_score,
        },
        "summary": _summary_dict(result),
        "participant_scores": [participant_entry],
        "computed_overall_risk": final_score,
        "displayed_overall_risk": final_score,
    }
    return response


if __name__ == "__main__":
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)

# Deployment quick notes:
# - Render.com: create a Web Service, point to repo, start command `uvicorn api_server:app --host 0.0.0.0 --port 8000`
# - Railway.app: create a service from repo, set same start command, expose 8000
# - Fly.io: use `fly launch` with Dockerfile; set internal port 8000 in fly.toml
# - GitHub Codespaces: run `uvicorn api_server:app --host 0.0.0.0 --port 8000` and forward port 8000
