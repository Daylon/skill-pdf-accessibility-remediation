"""
lang_detect.py — infer the primary language of a document and flag passages
that read as another language (WCAG 3.1.1 / 3.1.2, techniques PDF16 / PDF19).

Uses lingua (offline, good on short strings) when installed, otherwise a
stop-word vote. Only primary subtags can be inferred from text ("fr", "en");
with_variant() adds the default region used when nothing says otherwise
(fr -> fr-FR, en -> en-US). A region stated by the author or the source wins.
"""
from __future__ import annotations

import re
from functools import lru_cache

STOP = {
    "fr": "le la les des de du et un une est pour avec sur dans par au aux en ne pas que qui ce cette son ses".split(),
    "en": "the and of to in is for with on that by this as are be or from at an it was".split(),
    "de": "der die das und ist nicht mit für auf den dem ein eine zu von im sich des".split(),
    "es": "el la los las de y que en un una para con por es del se al lo como".split(),
    "it": "il lo la gli le di e che un una per con non sono del della nel".split(),
    "nl": "de het een en van is niet met voor op dat zijn te in aan".split(),
    "pt": "o a os as de e que em um uma para com não do da dos das por".split(),
}
# A language missing from the set is reported as the closest one in it, so Latin is included for passages:
# lorem-ipsum placeholder text then reads as Latin (and is not reported) instead of as Spanish or Italian.
# It never competes for the primary language.
DEFAULT_LANGS = ("fr", "en", "de", "es", "it", "nl", "pt", "ca", "pl", "sv", "la")
PRIMARY_LANGS = tuple(l for l in DEFAULT_LANGS if l != "la")
IGNORED_PARTS = {"la"}       # placeholder text or established Latin phrases (et al., per se): exempt
# region used when the primary language is known but the variant is not
DEFAULT_VARIANT = {"fr": "fr-FR", "en": "en-US", "de": "de-DE", "es": "es-ES", "it": "it-IT",
                   "nl": "nl-NL", "pt": "pt-PT", "ca": "ca-ES", "pl": "pl-PL", "sv": "sv-SE"}


def with_variant(code):
    """'fr' -> 'fr-FR', 'en' -> 'en-US'; a tag that already has a region is kept."""
    if not code:
        return code
    return code if "-" in code else DEFAULT_VARIANT.get(code.lower(), code.lower())


@lru_cache(maxsize=4)
def _detector(langs=DEFAULT_LANGS):
    try:
        from lingua import IsoCode639_1, Language, LanguageDetectorBuilder
    except ImportError:
        return None
    ls = [Language.from_iso_code_639_1(getattr(IsoCode639_1, code.upper())) for code in langs]
    return LanguageDetectorBuilder.from_languages(*ls).build()


def scores(text, langs=DEFAULT_LANGS):
    """Return [(lang, confidence)] best first. Confidence is 0..1."""
    det = _detector(tuple(langs))
    if det is not None and text.strip():
        vals = det.compute_language_confidence_values(text)
        return [(v.language.iso_code_639_1.name.lower(), float(v.value)) for v in vals]
    words = re.findall(r"[a-zà-ÿœæ]+", text.lower())
    raw = {k: sum(words.count(w) for w in set(v)) for k, v in STOP.items() if k in langs}
    tot = sum(raw.values()) or 1
    return sorted(((k, v / tot) for k, v in raw.items()), key=lambda kv: -kv[1])


def engine():
    return "lingua" if _detector(DEFAULT_LANGS) is not None else "stop-words"


def primary(text, langs=PRIMARY_LANGS):
    s = scores(text, langs)
    return s[0] if s else (None, 0.0)


def foreign_passages(units, doc_lang, min_conf=0.5, min_margin=0.3, min_letters=8, langs=DEFAULT_LANGS):
    """
    units: iterable of (id, text). Returns [(id, text, lang, conf)] for units
    that confidently read as a language other than doc_lang. Proper names and
    jargon are exempt from 3.1.2, so these are *suggestions* for a human.
    """
    base = (doc_lang or "").split("-")[0].lower()
    out = []
    for uid, text in units:
        t = text.strip()
        if len(re.sub(r"[^A-Za-zÀ-ÿ]", "", t)) < min_letters:
            continue
        words = re.findall(r"[A-Za-zÀ-ÿ]{2,}", t)
        if words and sum(w[0].isupper() for w in words) / len(words) >= 0.6:
            continue                      # mostly capitalised: proper names, exempt from 3.1.2
        s = scores(t, langs)
        if not s:
            continue
        top, conf = s[0]
        second = s[1][1] if len(s) > 1 else 0.0
        if top != base and top not in IGNORED_PARTS and conf >= min_conf and conf - second >= min_margin:
            out.append((uid, t, with_variant(top), round(conf, 2)))
    return out
