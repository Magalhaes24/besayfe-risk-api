"""
OCR-based input flow to build ProductInfo from images (labels, menus, sheets).
"""

from __future__ import annotations

# Standard library helpers for environment inspection and typing.
import os
import re
from typing import Dict, List, Optional, Tuple

# Third-party HTTP client for OCR.space API calls.
import io

import requests
from PIL import Image

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
        text, ocr_error, _ = self._extract_text(image_bytes)
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

        # Extract only ingredient-relevant sections instead of storing the full OCR dump.
        ingredients_text, contamination_text = self._extract_ingredient_sections(texts)
        raw_payload: Dict[str, Optional[str]] = {
            "ingredients_text": ingredients_text or None,
            "contamination_text": contamination_text or None,
            "ocr_space_error": ocr_error,
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
    def _extract_ingredient_sections(texts: List[str]) -> Tuple[str, str]:
        """
        Parse OCR text lines and extract only ingredient-relevant content.

        Returns (ingredients_text, contamination_text) where:
        - ingredients_text: lines belonging to the declared ingredient list.
        - contamination_text: lines with allergen/contamination declarations
          (contains, may contain, traces, facility warnings).
        """
        _INGREDIENT_HEADER = re.compile(
            r"^(ingredient[s]?|ingrediente[s]?|ingr\.?)\s*:?",
            re.IGNORECASE,
        )
        _CONTAMINATION = re.compile(
            r"\b(contains?|may contain|allergen[s]?|trace[s]?|"
            r"produced in a facility|fabricado|cont[eé]m|pode conter)\b",
            re.IGNORECASE,
        )
        _SECTION_BREAK = re.compile(
            r"^(nutrition|nutritional|valeur|n[uú]trition|storage|conserv|"
            r"best before|servings?|calories?|per serving|net weight|poids|"
            r"expiry|manufactured|directions?|preparation)\b",
            re.IGNORECASE,
        )

        ingredient_lines: List[str] = []
        contamination_lines: List[str] = []
        in_ingredients = False

        for line in texts:
            if _INGREDIENT_HEADER.match(line):
                in_ingredients = True
                ingredient_lines.append(line)
            elif in_ingredients and _SECTION_BREAK.match(line):
                in_ingredients = False
            elif in_ingredients:
                ingredient_lines.append(line)

            if _CONTAMINATION.search(line) and not _INGREDIENT_HEADER.match(line):
                contamination_lines.append(line)

        return " ".join(ingredient_lines).strip(), " ".join(contamination_lines).strip()

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

    @staticmethod
    def _compress_image(image_bytes: bytes, max_kb: int = 900) -> bytes:
        """Resize and compress image to stay within the OCR.space size limit."""
        if len(image_bytes) <= max_kb * 1024:
            return image_bytes
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        quality = 85
        scale = 1.0
        while True:
            w, h = int(img.width * scale), int(img.height * scale)
            resized = img.resize((w, h), Image.LANCZOS) if scale < 1.0 else img
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=quality)
            if len(buf.getvalue()) <= max_kb * 1024:
                return buf.getvalue()
            # Alternate between reducing quality and reducing size.
            if quality > 60:
                quality -= 10
            else:
                scale *= 0.85

    def _call_ocr_space(self, image_bytes: bytes) -> Tuple[str, Optional[str], Optional[Dict]]:
        # Compress if needed, then build the OCR.space request payload.
        image_bytes = self._compress_image(image_bytes)
        files = {"filename": ("image.jpg", image_bytes)}
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
