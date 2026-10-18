"""Locale selection (he/en) and text-direction helpers."""
from __future__ import annotations

from pathlib import Path

from flask import current_app, request
from flask_babel import Babel

SUPPORTED = ("en", "he")
RTL = {"he"}
COOKIE = "lang"


def select_locale() -> str:
    """?lang= -> cookie -> CLI --lang -> Accept-Language -> en."""
    for cand in (request.args.get("lang"), request.cookies.get(COOKIE),
                 current_app.config.get("DEFAULT_LANG")):
        if cand in SUPPORTED:
            return cand
    return request.accept_languages.best_match(SUPPORTED) or "en"


def text_dir(locale: str) -> str:
    return "rtl" if str(locale) in RTL else "ltr"


def init_babel(app) -> Babel:
    app.config.setdefault(
        "BABEL_TRANSLATION_DIRECTORIES", str(Path(__file__).parent / "translations"))
    return Babel(app, locale_selector=select_locale)
