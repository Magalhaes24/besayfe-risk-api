"""
Allergen metadata and helpers.

Defines the Annex II allergen set with multilingual labels, OpenFoodFacts tag
aliases, and utilities to resolve free-form inputs to canonical allergen codes.
"""

from __future__ import annotations

# Standard library text normalization and typing helpers.
import re
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
        "keywords": [
            "gluten",
            "wheat",
            "barley",
            "rye",
            "oats",
            "spelt",
            "durum",
            "kamut",
            "triticale",
            "bulgur",
            "couscous",
            "semolina",
            "farro",
            "seitan",
            "malt",
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
        "keywords": [
            "crustacean",
            "crab",
            "crabs",
            "shrimp",
            "shrimps",
            "prawn",
            "prawns",
            "lobster",
            "lobsters",
            "crayfish",
            "langoustine",
            "krill",
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
        "keywords": [
            "egg",
            "eggs",
            "albumen",
            "albumin",
            "ovalbumin",
            "ovomucoid",
            "yolk",
            "eggwhite",
            "eggwhites",
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
        "keywords": [
            "fish",
            "salmon",
            "tuna",
            "cod",
            "haddock",
            "pollock",
            "anchovy",
            "anchovies",
            "sardine",
            "sardines",
            "trout",
            "mackerel",
            "herring",
            "tilapia",
            "snapper",
            "bass",
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
        "keywords": [
            "peanut",
            "peanuts",
            "groundnut",
            "groundnuts",
            "monkey nut",
            "monkey nuts",
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
        "keywords": [
            "soy",
            "soya",
            "soybean",
            "soybeans",
            "edamame",
            "tofu",
            "tempeh",
            "miso",
            "shoyu",
            "tamari",
            "natto",
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
        "keywords": [
            "milk",
            "lactose",
            "butter",
            "cream",
            "cheese",
            "whey",
            "casein",
            "caseinate",
            "milkpowder",
            "powderedmilk",
            "skimmed",
            "yoghurt",
            "yogurt",
            "ghee",
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
        "keywords": [
            "tree nut",
            "tree nuts",
            "almond",
            "almonds",
            "hazelnut",
            "hazelnuts",
            "walnut",
            "walnuts",
            "pecan",
            "pecans",
            "cashew",
            "cashews",
            "pistachio",
            "pistachios",
            "macadamia",
            "macadamias",
            "brazil nut",
            "brazil nuts",
            "pine nut",
            "pine nuts",
            "chestnut",
            "chestnuts",
        ],
    },
    "CELERY": {
        "en": "Celery and products thereof",
        "pt": "Aipo e produtos à base de aipo",
        "off_tags": [
            "en:celery",
            "pt:aipo",
        ],
        "keywords": [
            "celery",
            "celeriac",
        ],
    },
    "MUSTARD": {
        "en": "Mustard and products thereof",
        "pt": "Mostarda e produtos à base de mostarda",
        "off_tags": [
            "en:mustard",
            "pt:mostarda",
        ],
        "keywords": [
            "mustard",
            "mustardseed",
            "mustardseeds",
            "dijon",
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
        "keywords": [
            "sesame",
            "sesameseed",
            "sesameseeds",
            "tahini",
            "benne",
            "gingelly",
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
        "keywords": [
            "sulphite",
            "sulphites",
            "sulfite",
            "sulfites",
            "sulphur dioxide",
            "sulfur dioxide",
            "e220",
            "e221",
            "e222",
            "e223",
            "e224",
            "e226",
            "e227",
            "e228",
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
        "keywords": [
            "lupin",
            "lupine",
            "tremoco",
            "tremoço",
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
        "keywords": [
            "mollusc",
            "molluscs",
            "mollusk",
            "mollusks",
            "clam",
            "clams",
            "mussel",
            "mussels",
            "oyster",
            "oysters",
            "squid",
            "octopus",
            "cuttlefish",
            "snail",
            "whelk",
            "cockle",
            "scallop",
            "abalone",
        ],
    },
}


def _build_off_tag_mapping(allergens: Dict[str, Dict[str, object]]) -> Dict[str, str]:
    """Flatten OpenFoodFacts tags to canonical allergen codes."""
    # Map each OFF tag (lowercase) to the internal canonical code.
    mapping: Dict[str, str] = {}
    for code, meta in allergens.items():
        for tag in meta.get("off_tags", []):
            mapping[tag.lower()] = code
    return mapping


# Lower-case OpenFoodFacts tag -> internal allergen code
OFF_TAG_TO_CODE: Dict[str, str] = _build_off_tag_mapping(ANNEX_II_ALLERGENS)


def _normalize(text: str) -> str:
    """Lowercase, strip accents, and trim whitespace."""
    # Strip accents and normalize whitespace for matching.
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.lower().strip()


def _tokenize_ingredient_text(text: str) -> List[str]:
    """Normalize ingredient text and split into distinct alphanumeric tokens."""
    # Replace non-alphanumerics with spaces and keep meaningful tokens.
    normalized = re.sub(r"[^a-z0-9]+", " ", _normalize(text))
    return [tok for tok in normalized.split() if len(tok) > 2]


def _build_synonym_mapping(allergens: Dict[str, Dict[str, object]]) -> Dict[str, str]:
    """Map any synonym (label, tag, code) to the canonical allergen code."""
    # Build a normalized synonym lookup table for resolution.
    mapping: Dict[str, str] = {}
    for code, meta in allergens.items():
        mapping[_normalize(code)] = code
        for lang, label in meta.items():
            if lang in ("off_tags", "keywords"):
                continue
            mapping[_normalize(str(label))] = code
        for tag in meta.get("off_tags", []):
            mapping[_normalize(tag)] = code
            if ":" in tag:
                mapping[_normalize(tag.split(":", 1)[1])] = code
        for keyword in meta.get("keywords", []):
            mapping[_normalize(keyword)] = code
    return mapping


# Any normalized synonym (tag, label, code) -> canonical code
SYNONYM_TO_CODE: Dict[str, str] = _build_synonym_mapping(ANNEX_II_ALLERGENS)


def resolve_allergen_code(user_input: str) -> Optional[str]:
    """
    Resolve free-form allergen text (any language) to a canonical code.
    Falls back to None if we cannot map it.
    """
    # Normalize user input to a key that matches the synonym map.
    key = _normalize(user_input)
    return SYNONYM_TO_CODE.get(key)


def detect_allergens_in_ingredient_texts(ingredient_texts: List[str]) -> List[str]:
    """
    Scan a list of ingredient strings and return the set of Annex II allergen codes found.
    """
    # Tokenize all ingredient text into a flat list.
    tokens: List[str] = []
    for text in ingredient_texts:
        tokens.extend(_tokenize_ingredient_text(text))

    # Build candidate phrases (unigrams + bigrams + trigrams) so multi-word
    # ingredients like "brazil nut" or "milk powder" can be matched.
    candidates = list(tokens)
    for size in (2, 3):
        for i in range(len(tokens) - size + 1):
            candidates.append(" ".join(tokens[i : i + size]))

    # Resolve candidates to allergen codes and dedupe results.
    found: List[str] = []
    seen = set()
    for token in set(candidates):
        code = resolve_allergen_code(token)
        if not code or code in seen:
            continue
        seen.add(code)
        found.append(code)

    return sorted(found)


def allergen_label(code: str, lang: str = "en") -> str:
    """
    Return a human-friendly allergen label in the requested language, defaulting to English.
    """
    if not code:
        return ""
    # Choose the requested language label, falling back to English or code.
    meta = ANNEX_II_ALLERGENS.get(code.upper())
    if not meta:
        return code
    return meta.get(lang) or meta.get("en") or code
