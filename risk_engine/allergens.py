"""
Allergen metadata and helpers.

Defines the Annex II allergen set with multilingual labels, OpenFoodFacts tag
aliases, and utilities to resolve free-form inputs to canonical allergen codes.
"""

from __future__ import annotations

import unicodedata
from typing import Dict, List, Optional

# Annex II (EU) allergen definitions with English and European Portuguese labels and
# OpenFoodFacts tag aliases to normalize inbound data.
ANNEX_II_ALLERGENS: Dict[str, Dict[str, object]] = {
    "GLUTEN": {
        "en": "Cereals containing gluten (wheat, rye, barley, oats and derivatives)",
        "pt": "Cereais que contêm glúten (trigo, centeio, cevada, aveia e derivados)",
        "off_tags": [
            "en:gluten",
            "pt:gluten",
        ],
    },
    "CRUSTACEANS": {
        "en": "Crustaceans and products thereof",
        "pt": "Crustáceos e produtos à base de crustáceos",
        "off_tags": [
            "en:crustaceans",
            "en:crustacean",
            "pt:crustaceos",
            "pt:crustaceo",
        ],
    },
    "EGG": {
        "en": "Eggs and products thereof",
        "pt": "Ovos e produtos à base de ovo",
        "off_tags": [
            "en:egg",
            "en:eggs",
            "pt:ovo",
            "pt:ovos",
        ],
    },
    "FISH": {
        "en": "Fish and products thereof",
        "pt": "Peixe e produtos à base de peixe",
        "off_tags": [
            "en:fish",
            "pt:peixe",
            "pt:peixes",
        ],
    },
    "PEANUT": {
        "en": "Peanuts and products thereof",
        "pt": "Amendoim e produtos à base de amendoim",
        "off_tags": [
            "en:peanuts",
            "en:peanut",
            "pt:amendoim",
            "pt:amendoins",
        ],
    },
    "SOY": {
        "en": "Soybeans and products thereof",
        "pt": "Soja e produtos à base de soja",
        "off_tags": [
            "en:soybeans",
            "en:soy",
            "en:soya",
            "pt:soja",
        ],
    },
    "MILK": {
        "en": "Milk and dairy products including lactose",
        "pt": "Leite e produtos lácteos, incluindo lactose",
        "off_tags": [
            "en:milk",
            "en:milk-protein",
            "en:lactose",
            "pt:leite",
            "pt:lactose",
        ],
    },
    "TREE_NUTS": {
        "en": "Nuts (almond, hazelnut, walnut, cashew, pecan, Brazil nut, pistachio, macadamia)",
        "pt": "Frutos de casca rija (amêndoa, avelã, noz, caju, pecã, castanha do Brasil, pistácio, macadâmia)",
        "off_tags": [
            "en:nuts",
            "en:tree-nuts",
            "en:almonds",
            "en:hazelnuts",
            "en:walnuts",
            "en:cashew",
            "en:pistachio",
            "pt:frutos-de-casca-rija",
            "pt:frutos-de-casca-dura",
            "pt:amendoa",
            "pt:amendoas",
            "pt:avelas",
            "pt:noz",
            "pt:nozes",
            "pt:caju",
            "pt:pistacio",
        ],
    },
    "CELERY": {
        "en": "Celery and products thereof",
        "pt": "Aipo e produtos à base de aipo",
        "off_tags": [
            "en:celery",
            "pt:aipo",
        ],
    },
    "MUSTARD": {
        "en": "Mustard and products thereof",
        "pt": "Mostarda e produtos à base de mostarda",
        "off_tags": [
            "en:mustard",
            "pt:mostarda",
        ],
    },
    "SESAME": {
        "en": "Sesame seeds and products thereof",
        "pt": "Sementes de sésamo e produtos à base de sésamo",
        "off_tags": [
            "en:sesame",
            "en:sesame-seeds",
            "pt:sesamo",
            "pt:sementes-de-sesamo",
        ],
    },
    "SULPHITES": {
        "en": "Sulphur dioxide and sulphites >10mg/kg or 10mg/L",
        "pt": "Dióxido de enxofre e sulfitos em concentração superior a 10mg/kg ou 10mg/L",
        "off_tags": [
            "en:sulphur-dioxide-and-sulphites",
            "en:sulphites",
            "en:sulfites",
            "pt:dioxido-de-enxofre-e-sulfitos",
            "pt:sulfitos",
        ],
    },
    "LUPIN": {
        "en": "Lupin and products thereof",
        "pt": "Tremoço e produtos à base de tremoço",
        "off_tags": [
            "en:lupin",
            "en:lupine",
            "pt:tremoço",
            "pt:tremoco",
        ],
    },
    "MOLLUSCS": {
        "en": "Molluscs and products thereof",
        "pt": "Moluscos e produtos à base de moluscos",
        "off_tags": [
            "en:molluscs",
            "en:mollusks",
            "pt:moluscos",
        ],
    },
}


def _build_off_tag_mapping(allergens: Dict[str, Dict[str, object]]) -> Dict[str, str]:
    """Flatten OpenFoodFacts tags to canonical allergen codes."""
    mapping: Dict[str, str] = {}
    for code, meta in allergens.items():
        for tag in meta.get("off_tags", []):
            mapping[tag.lower()] = code
    return mapping


# Lower-case OpenFoodFacts tag -> internal allergen code
OFF_TAG_TO_CODE: Dict[str, str] = _build_off_tag_mapping(ANNEX_II_ALLERGENS)


def _normalize(text: str) -> str:
    """Lowercase, strip accents, and trim whitespace."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.lower().strip()


def _build_synonym_mapping(allergens: Dict[str, Dict[str, object]]) -> Dict[str, str]:
    """Map any synonym (label, tag, code) to the canonical allergen code."""
    mapping: Dict[str, str] = {}
    for code, meta in allergens.items():
        mapping[_normalize(code)] = code
        for lang, label in meta.items():
            if lang in ("off_tags",):
                continue
            mapping[_normalize(str(label))] = code
        for tag in meta.get("off_tags", []):
            mapping[_normalize(tag)] = code
            if ":" in tag:
                mapping[_normalize(tag.split(":", 1)[1])] = code
    return mapping


# Any normalized synonym (tag, label, code) -> canonical code
SYNONYM_TO_CODE: Dict[str, str] = _build_synonym_mapping(ANNEX_II_ALLERGENS)


def resolve_allergen_code(user_input: str) -> Optional[str]:
    """
    Resolve free-form allergen text (any language) to a canonical code.
    Falls back to None if we cannot map it.
    """
    key = _normalize(user_input)
    return SYNONYM_TO_CODE.get(key)


def allergen_label(code: str, lang: str = "en") -> str:
    """
    Return a human-friendly allergen label in the requested language, defaulting to English.
    """
    if not code:
        return ""
    meta = ANNEX_II_ALLERGENS.get(code.upper())
    if not meta:
        return code
    return meta.get(lang) or meta.get("en") or code
