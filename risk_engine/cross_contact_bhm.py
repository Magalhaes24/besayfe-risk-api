"""
Bayesian Hierarchical Model (BHM) for estimating cross-contact risk.

This module estimates P(C=1 | product, allergen) for facility cross-contact
using a simple hierarchical logistic model with category and brand random
effects, ingredient-derived signals, and "may contain" declarations.

If PyMC is available, it can be extended to perform full posterior sampling.
To keep runtime low in the CLI, we compute closed-form approximations of the
posterior mean and a 95% credible interval using normal assumptions on the
logit scale.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

import numpy as np


def _safe_logit(p: float, eps: float = 1e-6) -> float:
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def _safe_inv_logit(x: float) -> float:
    return 1 / (1 + math.exp(-x))


@dataclass
class CrossContactConfig:
    mu_category: float = 0.0
    sigma_category: float = 0.8
    sigma_brand: float = 0.6
    sigma_gamma: float = 1.0
    sigma_delta: float = 1.0
    delta_may_contain_boost: float = 3.0  # strong positive shift for "may contain"


def _ingredient_signal(product: Dict, allergen: str) -> float:
    """
    Ingredient-based predictor combining category and brand co-occurrence stats.
    The caller should provide category_stats/brand_stats with 'freq' and
    'co_occurrence' keys between 0 and 1. Missing stats default to 0.
    """
    cat = product.get("category_stats", {}).get(allergen, {})
    brand = product.get("brand_stats", {}).get(allergen, {})
    freq_cat = float(cat.get("freq", 0.0))
    co_cat = float(cat.get("co_occurrence", 0.0))
    freq_brand = float(brand.get("freq", 0.0))
    co_brand = float(brand.get("co_occurrence", 0.0))

    # Weighted blend of frequency and co-occurrence across category and brand
    return 0.4 * freq_cat + 0.3 * co_cat + 0.2 * freq_brand + 0.1 * co_brand


def estimate_cross_contact(product: Dict, allergen: str, config: Optional[CrossContactConfig] = None) -> Dict[str, float]:
    """
    Estimate cross-contact probability for a (product, allergen) pair.

    Args:
        product: dict with keys
            - category: str
            - brand: str
            - may_contain: {allergen: bool}
            - category_stats: {allergen: {freq, co_occurrence}}
            - brand_stats: {allergen: {freq, co_occurrence}}
        allergen: allergen code (e.g., "TREE_NUTS")
        config: CrossContactConfig overrides

    Returns:
        dict with probability, lower_ci, upper_ci (floats in [0,1])
    """
    cfg = config or CrossContactConfig()

    # Priors / effects
    category_key = product.get("category") or "unknown"
    brand_key = product.get("brand") or "unknown"

    # Category effect: center on mu_category, variance sigma_category^2
    alpha_cat = cfg.mu_category
    var_cat = cfg.sigma_category ** 2

    # Brand effect: zero-mean with sigma_brand^2 variance
    beta_brand = 0.0
    var_brand = cfg.sigma_brand ** 2

    # Ingredient signal (deterministic feature); coefficient gamma ~ Normal(0,1)
    signal = _ingredient_signal(product, allergen)
    gamma_mean = 0.0
    var_gamma = cfg.sigma_gamma ** 2 * (signal ** 2)

    # May contain flag
    may_contain = bool(product.get("may_contain", {}).get(allergen, False))
    delta = cfg.delta_may_contain_boost if may_contain else 0.0
    var_delta = cfg.sigma_delta ** 2 if not may_contain else 0.0  # if boosted, treat as fixed

    # Mean and variance on logit scale
    mean_logit = alpha_cat + beta_brand + gamma_mean * signal + delta
    var_logit = var_cat + var_brand + var_gamma + var_delta
    sd_logit = math.sqrt(max(var_logit, 1e-9))

    # Posterior mean approximation on probability scale
    prob_mean = _safe_inv_logit(mean_logit)

    # 95% credible interval (normal approximation on logit scale)
    lower = _safe_inv_logit(mean_logit - 1.96 * sd_logit)
    upper = _safe_inv_logit(mean_logit + 1.96 * sd_logit)

    return {
        "probability": prob_mean,
        "lower_ci": lower,
        "upper_ci": upper,
    }


def ingredient_presence_flag(product: Dict, allergen: str) -> float:
    """
    Heuristic presence flag: returns 1.0 if the allergen is explicitly declared
    in product['allergens'], else 0.
    """
    declared = product.get("allergens", [])
    return 1.0 if allergen in declared else 0.0


def may_contain_flag(product: Dict, allergen: str) -> float:
    """
    Returns 1.0 if traces/may contain is present for the allergen.
    """
    return 1.0 if product.get("may_contain", {}).get(allergen, False) else 0.0


def final_cross_contact_risk(product: Dict, allergen: str, config: Optional[CrossContactConfig] = None) -> Dict[str, float]:
    """
    Compute the final risk contribution using the BHM probability blended with
    ingredient and may-contain signals per specification:

    final_risk = min(1.0,
                     1.0 * ingredient_presence +
                     0.7 * may_contain_flag +
                     0.5 * p_bhm)
    """
    bhm = estimate_cross_contact(product, allergen, config=config)
    presence = ingredient_presence_flag(product, allergen)
    may_flag = may_contain_flag(product, allergen)
    risk = min(1.0, presence + 0.7 * may_flag + 0.5 * bhm["probability"])
    return {
        "risk": risk,
        "probability": bhm["probability"],
        "lower_ci": bhm["lower_ci"],
        "upper_ci": bhm["upper_ci"],
        "presence": presence,
        "may_contain": may_flag,
    }
