"""
OCR-based input flow to build ProductInfo from images (labels, menus, sheets).
"""

from __future__ import annotations

# Standard library helpers for environment inspection and typing.
import os
from typing import Dict, List, Optional, Tuple

# Third-party HTTP client for OCR.space API calls.
import requests

from .allergens import detect_allergens_in_ingredient_texts
from .models import AllergenFact, PresenceType, ProductInfo


class ImageTextProductSource:
    """
    Extract ingredient text from an image and build ProductInfo for the risk engine.
    """

    OCR_SPACE_ENDPOINT = "https://api.ocr.space/parse/image"
    DEFAULT_API_KEY = "K87148622888957"

    def __init__(
        self,
        lang: str = "eng",
        tesseract_cmd: Optional[str] = None,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: float = 30.0,
    ):
        # Store OCR configuration for OCR.space (Tesseract config is ignored).
        self.lang = lang
        self.api_key = api_key or os.environ.get("OCR_SPACE_API_KEY") or self.DEFAULT_API_KEY
        self.endpoint = endpoint or self.OCR_SPACE_ENDPOINT
        self.timeout = timeout
        self.tesseract_cmd = tesseract_cmd

    def product_from_image(
        self,
        image_bytes: bytes,
        reference_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> ProductInfo:
        # Run OCR, normalize text into lines, and convert them into allergen facts.
        text, ocr_error, ocr_payload = self._extract_text(image_bytes)
        texts = self._split_text(text)
        facts = self._facts_from_texts(texts)

        # Track data-quality notes for callers to understand OCR coverage.
        data_notes: List[str] = []
        if ocr_error:
            data_notes.append(f"OCR.space error: {ocr_error}")
        if not texts:
            data_notes.append(
                "OCR did not extract any readable text from the image; no ingredient analysis possible"
            )
        elif texts and not facts:
            data_notes.append("OCR text analyzed and no allergens detected in available data")

        # Preserve both the raw OCR text and the normalized ingredient string.
        raw_payload = {
            "ingredients_text": " ".join(texts).strip(),
            "ocr_text": text,
            "ocr_space_error": ocr_error,
            "ocr_space_payload": ocr_payload,
        }

        # Build the ProductInfo using the OCR-derived allergens and metadata.
        return ProductInfo(
            ean=reference_id or "image-input",
            name=name or "Image input",
            brand=None,
            source="image_ocr",
            allergen_facts=facts,
            raw_payload=raw_payload,
            traces_tags=[],
            data_notes=data_notes,
        )

    def _extract_text(self, image_bytes: bytes) -> Tuple[str, Optional[str], Optional[Dict]]:
        # Send the image to OCR.space and parse its text response.
        return self._call_ocr_space(image_bytes)

    @staticmethod
    def _split_text(text: str) -> List[str]:
        # Strip whitespace and drop empty lines to keep only meaningful entries.
        lines = [line.strip() for line in text.splitlines()]
        return [line for line in lines if line]

    @staticmethod
    def _facts_from_texts(texts: List[str]) -> List[AllergenFact]:
        # Detect allergens in the OCR text and emit a standard AllergenFact list.
        facts: List[AllergenFact] = []
        detected_codes = detect_allergens_in_ingredient_texts(texts)
        for code in detected_codes:
            facts.append(
                AllergenFact(
                    allergen_code=code,
                    presence_type=PresenceType.CONTAINS,
                    source="image_ocr:ingredients_text",
                    weight=1.0,
                    confidence=1.0,
                )
            )
        return facts

    def _call_ocr_space(self, image_bytes: bytes) -> Tuple[str, Optional[str], Optional[Dict]]:
        # Build the OCR.space request payload.
        files = {"filename": ("image.png", image_bytes)}
        data = {
            "apikey": self.api_key,
            "language": self.lang,
            "OCREngine": "2",
        }

        try:
            response = requests.post(
                self.endpoint, files=files, data=data, timeout=self.timeout
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # pragma: no cover - network errors handled at runtime
            return "", str(exc), None

        if payload.get("IsErroredOnProcessing"):
            errors = payload.get("ErrorMessage") or []
            error_text = "; ".join(errors) if errors else payload.get("ErrorDetails") or "OCR.space failed"
            return "", error_text, payload

        parsed_results = payload.get("ParsedResults") or []
        text_parts = [result.get("ParsedText", "") for result in parsed_results]
        text = "\n".join(part for part in text_parts if part).strip()
        return text, None, payload
