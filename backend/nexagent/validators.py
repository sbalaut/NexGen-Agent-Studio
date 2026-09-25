"""Deterministic answer validators (review point 2).

These run without any AI model. They check that every plant-specific claim in a
draft answer is cited, and that each number/unit in a claim is found in the
*cited* evidence bound to the same equipment tag, the same condition
(normal / startup / trip / design ...), the same inequality direction and the
same negation. A number appearing somewhere in a document is not enough.

They are deliberately conservative: a doubtful answer goes to an engineer
rather than to the user. They do not prove an answer is correct.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .documents import TAG_RE

# ------------------------------------------------------------------ patterns
_UNIT_ALIASES = {
    "°c": "degc", "ºc": "degc", "degc": "degc", "deg c": "degc", "deg.c": "degc", "c": "degc",
    "°f": "degf", "degf": "degf", "k": "kelvin",
    "bar": "bar", "bara": "bara", "barg": "barg", "bar(g)": "barg", "bar g": "barg", "bar(a)": "bara",
    "kg/cm2": "kg/cm2", "kg/cm²": "kg/cm2", "kg/cm2g": "kg/cm2g", "kg/cm²g": "kg/cm2g", "kg/cm2(g)": "kg/cm2g",
    "kg/cm2a": "kg/cm2a", "ksc": "kg/cm2", "kscg": "kg/cm2g",
    "psi": "psi", "psig": "psig", "psia": "psia", "kpa": "kpa", "mpa": "mpa", "pa": "pa",
    "mmhg": "mmhg", "mmh2o": "mmh2o", "mmwc": "mmh2o", "torr": "mmhg",
    "%": "pct", "wt%": "wt%", "vol%": "vol%", "mol%": "mol%", "ppm": "ppm", "ppmw": "ppmw", "ppmv": "ppmv", "ppb": "ppb",
    "m3/h": "m3/h", "m3/hr": "m3/h", "m³/h": "m3/h", "m³/hr": "m3/h", "nm3/h": "nm3/h", "nm3/hr": "nm3/h",
    "t/h": "t/h", "t/hr": "t/h", "tph": "t/h", "mt/hr": "t/h", "mt/h": "t/h", "kg/h": "kg/h", "kg/hr": "kg/h",
    "bpsd": "bpsd", "kbpsd": "kbpsd", "bbl/d": "bpd", "bpd": "bpd", "mmtpa": "mmtpa",
    "mm": "mm", "cm": "cm", "m": "m", "rpm": "rpm", "kw": "kw", "mw": "mw", "kv": "kv", "a": "amp", "amp": "amp",
    "v": "volt", "cp": "cp", "cst": "cst", "mg/l": "mg/l", "mv": "mv", "kcal/kg": "kcal/kg",
    "s": "s", "sec": "s", "seconds": "s", "min": "min", "minutes": "min", "h": "hour", "hr": "hour", "hrs": "hour",
    "hours": "hour", "days": "day",
}
_UNIT_RE = "|".join(sorted((re.escape(u) for u in _UNIT_ALIASES), key=len, reverse=True))
_NUM = r"[-−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-−]?\d+(?:\.\d+)?"
QUANTITY_RE = re.compile(
    rf"(?P<cmp>>=|<=|≥|≤|>|<)?\s*(?P<a>{_NUM})(?:\s*(?:-|–|to|~)\s*(?P<b>{_NUM}))?\s*(?P<unit>{_UNIT_RE})(?![A-Za-z0-9/])",
    re.IGNORECASE)
CITE_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

_CMP_WORDS = {
    "gt": ("above", "more than", "greater than", "exceed", "exceeds", "higher than", "over", "at least",
           "minimum", "min.", "not less than", "से अधिक", "से ज्यादा"),
    "lt": ("below", "less than", "lower than", "under", "maximum", "max.", "not exceed", "not more than",
           "up to", "within", "से कम"),
}
_CONDITIONS = {
    "startup": ("startup", "start-up", "start up", "commissioning"),
    "shutdown": ("shutdown", "shut down", "shut-down"),
    "trip": ("trip", "interlock", "esd", "emergency"),
    "alarm": ("alarm",),
    "design": ("design", "rated", "mawp", "rating"),
    "normal": ("normal", "operating", "running", "usual"),
}
_NEGATIONS = ("not", "never", "no ", "don't", "do not", "must not", "shall not", "without", "cannot", "can't",
              "नहीं", "मत", "न करें")
_PROPERTIES = ("pressure", "temperature", "flow", "level", "density", "viscosity", "speed", "power", "current",
               "voltage", "concentration", "vibration", "differential", "dp", "capacity", "duty")


@dataclass
class Quantity:
    a: float
    b: float | None
    unit: str
    cmp: str | None   # 'gt' | 'lt' | None
    span: tuple[int, int]

    def same_value(self, other: "Quantity") -> bool:
        return self.unit == other.unit and self.a == other.a and self.b == other.b

    def same_magnitude_other_sign(self, other: "Quantity") -> bool:
        return self.unit == other.unit and abs(self.a) == abs(other.a) and self.a != other.a


@dataclass
class Issue:
    code: str
    claim: str
    explanation: str
    evidence_refs: list[int] = field(default_factory=list)
    blocking: bool = True
    source: str = "deterministic"

    def public(self) -> dict:
        return {"code": self.code, "claim": self.claim, "explanation": self.explanation,
                "evidence_refs": self.evidence_refs, "blocking": self.blocking, "source": self.source}


# ------------------------------------------------------------------ helpers
def _num(text: str) -> float:
    return float(text.replace("−", "-").replace(",", ""))


def _cmp_near(text: str, start: int, symbol: str | None) -> str | None:
    if symbol:
        return "gt" if symbol in (">", ">=", "≥") else "lt"
    window = text[max(0, start - 40):start].lower()
    hits = [(window.rfind(w), k) for k, words in _CMP_WORDS.items() for w in words if w in window]
    return max(hits)[1] if hits else None


_UNIT_COLUMN = re.compile(r"(\d)\s*\|\s*Units?\s*:\s*([^|]+?)\s*(?=\||$)", re.IGNORECASE)


def quantities(text: str) -> list[Quantity]:
    text = _UNIT_COLUMN.sub(lambda m: f"{m.group(1)} {m.group(2)}", text)   # table rows with a separate Unit column
    clean = TAG_RE.sub(lambda m: " " * len(m.group(0)), text)          # tag digits are not values
    clean = CITE_RE.sub(lambda m: " " * len(m.group(0)), clean)        # nor are citation markers
    out = []
    for m in QUANTITY_RE.finditer(clean):
        unit = _UNIT_ALIASES.get(m.group("unit").lower().replace(" ", " "), m.group("unit").lower())
        if unit in ("degc",) and m.group("unit").lower() == "c" and not re.search(r"°|deg", clean[max(0, m.start()-2):m.end()]):
            # a bare "C" is too ambiguous (could be a letter); require the degree sign or "deg"
            continue
        a = _num(m.group("a"))
        b = _num(m.group("b")) if m.group("b") else None
        # "10-20" style ranges must not be read as a negative second value
        if b is not None and b < 0:
            b = abs(b)
        out.append(Quantity(a, b, unit, _cmp_near(clean, m.start(), m.group("cmp")), m.span()))
    return out


def conditions(text: str) -> set[str]:
    t = text.lower()
    return {k for k, words in _CONDITIONS.items() if any(w in t for w in words)}


def negated(text: str) -> bool:
    t = " " + text.lower() + " "
    return any(w in t for w in (" not ", " never ", " no ", "n't ", " without ", " cannot ", "नहीं", " मत "))


def properties(text: str) -> set[str]:
    t = text.lower()
    return {p for p in _PROPERTIES if re.search(rf"\b{re.escape(p)}\b", t)}


def split_claims(answer: str) -> list[str]:
    parts = re.split(r"(?<=[.!?।])\s+(?=[A-Z0-9ऀ-ॿ\[\(\"'])|\n+", answer.strip())
    return [p.strip(" -*•\t") for p in parts if p and len(p.strip(" -*•\t")) > 1]


def is_heading(line: str) -> bool:
    """Markdown heading ('### Heading'), a fully bold line, or a short title ending with ':' and no values."""
    t = line.strip()
    if t.startswith("#"):
        return not quantities(t)
    if re.fullmatch(r"\*\*[^*]{1,100}\*\*:?", t) or (t.endswith(":") and len(t.split()) <= 8):
        return not quantities(t) and not CITE_RE.search(t)
    return False


def segments(evidence_text: str) -> list[str]:
    if " | " in evidence_text and "\n" not in evidence_text:
        return [evidence_text]                        # a table row is one binding unit
    return [s for s in re.split(r"(?<=[.!?।;])\s+|\n+", evidence_text) if s.strip()]


def devanagari_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(1 for c in letters if "ऀ" <= c <= "ॿ") / len(letters) if letters else 0.0


NOT_FOUND_MARKERS = ("not found in the accessible sources", "not available in the accessible sources",
                     "उपलब्ध स्रोतों में नहीं मिला")


def is_not_found(answer: str) -> bool:
    a = answer.lower()
    return any(m in a for m in NOT_FOUND_MARKERS)


# ------------------------------------------------------------------ main check
def validate_answer(question: str, answer: str, evidence: list[dict], *,
                    allowed_sections: list[str] | None = None) -> list[Issue]:
    """evidence: server-built items with keys ref, text, section_type, document_id, revision_no, equipment_tags."""
    issues: list[Issue] = []
    if not answer.strip():
        return [Issue("empty_answer", "", "The draft is empty.")]
    by_ref = {e["ref"]: e for e in evidence}
    if is_not_found(answer):
        return issues   # abstention is validated elsewhere (it cannot contain claims)

    all_tags = set().union(*(set(e.get("equipment_tags") or []) | set(TAG_RE.findall(e["text"])) for e in evidence)) \
        if evidence else set()
    any_citation = False
    cited_docs: dict[str, set[int]] = {}
    for claim in split_claims(answer):
        if is_heading(claim):
            for tag in sorted(set(TAG_RE.findall(claim)) - all_tags):     # headings carry no facts, but no invented tags
                issues.append(Issue("unknown_equipment_tag", claim,
                                    f"Equipment tag {tag} does not appear in any retrieved source."))
            continue
        refs = sorted({int(x) for m in CITE_RE.finditer(claim) for x in m.group(1).split(",")})
        bad = [r for r in refs if r not in by_ref]
        if bad:
            issues.append(Issue("invalid_citation", claim, f"Citation(s) {bad} do not refer to any retrieved source.", bad))
        cited = [by_ref[r] for r in refs if r in by_ref]
        any_citation = any_citation or bool(cited)
        for e in cited:
            cited_docs.setdefault(e["document_id"], set()).add(e["revision_no"])
            if allowed_sections and e["section_type"] not in allowed_sections:
                issues.append(Issue("section_out_of_scope", claim,
                                    f"Source [{e['ref']}] is a '{e['section_type']}' section, which is outside this "
                                    f"question's scope ({', '.join(allowed_sections)}).", [e["ref"]]))
        tags = set(TAG_RE.findall(CITE_RE.sub(" ", claim)))
        qs = quantities(claim)
        plant_specific = bool(tags or qs)
        if plant_specific and cited and all(e.get("section_type") == "graph_summary" for e in cited):
            issues.append(Issue("summary_not_primary_source", claim,
                                "A generated knowledge-graph summary is the only source cited for a specific value or "
                                "tag; cite the original document passage.", refs))
            continue
        cited = [e for e in cited if e.get("section_type") != "graph_summary"] or cited
        if plant_specific and not cited:
            issues.append(Issue("uncited_claim", claim, "This statement contains plant-specific tags or values "
                                                        "but cites no source."))
            continue
        for tag in sorted(tags):
            if tag not in all_tags:
                issues.append(Issue("unknown_equipment_tag", claim,
                                    f"Equipment tag {tag} does not appear in any retrieved source."))
            elif cited and not any(tag in e["text"] or tag in (e.get("equipment_tags") or []) for e in cited):
                issues.append(Issue("tag_not_in_cited_source", claim,
                                    f"{tag} is not mentioned in the cited source(s).", refs))
        claim_conditions = conditions(claim)
        claim_neg = negated(claim)
        claim_props = properties(claim)
        for q in qs:
            _check_quantity(claim, q, tags, claim_conditions, claim_neg, claim_props, cited, issues)
    if not any_citation and (quantities(answer) or TAG_RE.search(answer)):
        issues.append(Issue("no_citations", answer[:160], "The answer makes plant-specific statements without any citation."))
    for doc, revs in cited_docs.items():
        if len(revs) > 1:
            issues.append(Issue("conflicting_revisions", "", f"The answer cites revisions {sorted(revs)} of the same "
                                                             "document; only one revision may be used."))
    q_hi, a_hi = devanagari_share(question), devanagari_share(answer)
    if (q_hi > 0.5) != (a_hi > 0.5) and abs(q_hi - a_hi) > 0.4:
        issues.append(Issue("language_mismatch", "", "The answer is not in the language of the question.",
                            blocking=False))
    return _dedupe(issues)


def _check_quantity(claim, q, tags, claim_conditions, claim_neg, claim_props, cited, issues) -> None:
    refs = [e["ref"] for e in cited]
    exact: list[tuple[dict, str, Quantity]] = []
    unit_only, sign_only = [], []
    for e in cited:
        for seg in segments(e["text"]):
            for eq in quantities(seg):
                if eq.same_value(q):
                    exact.append((e, seg, eq))
                elif eq.a == q.a and eq.b == q.b:
                    unit_only.append(eq.unit)
                elif eq.same_magnitude_other_sign(q):
                    sign_only.append(e["ref"])
    label = f"{q.a:g}{'–' + format(q.b, 'g') if q.b is not None else ''} {q.unit}"
    if not exact:
        if sign_only:
            issues.append(Issue("sign_mismatch", claim, f"The value {label} has the opposite sign in the source.", sign_only))
        elif unit_only:
            issues.append(Issue("unit_mismatch", claim, f"The source gives this number with unit {unit_only[0]}, not {q.unit}.", refs))
        else:
            issues.append(Issue("value_not_in_cited_source", claim,
                                f"The value {label} is not stated in the cited source(s).", refs))
        return
    # binding: at least one exact match must be bound to the claim's equipment/condition/negation/inequality
    problems: list[Issue] = []
    for e, seg, eq in exact:
        local: list[Issue] = []
        if tags:
            other_tags = set(TAG_RE.findall(e["text"])) - tags
            bound = all(t in seg for t in tags) or (all(t in e["text"] for t in tags) and not other_tags)
            if not bound:
                seg_tags = sorted(set(TAG_RE.findall(seg)))
                local.append(Issue("equipment_value_binding", claim,
                                   f"In source [{e['ref']}] the value {label} belongs to "
                                   f"{', '.join(seg_tags) or 'another item'}, not {', '.join(sorted(tags))}.", [e["ref"]]))
        seg_props = properties(seg)
        if claim_props and seg_props and not (claim_props & seg_props):
            local.append(Issue("property_mismatch", claim,
                               f"Source [{e['ref']}] states {label} for {', '.join(sorted(seg_props))}, "
                               f"not {', '.join(sorted(claim_props))}.", [e["ref"]]))
        seg_cond = conditions(seg) | ({"startup"} if e["section_type"] == "startup" else set()) \
            | ({"shutdown"} if e["section_type"] == "shutdown" else set()) \
            | ({"trip"} if e["section_type"] == "interlock" else set())
        special = seg_cond - {"normal"}
        if special and not (claim_conditions & special):
            local.append(Issue("condition_mismatch", claim,
                               f"In source [{e['ref']}] {label} applies to {', '.join(sorted(special))} conditions; "
                               "the answer does not say so.", [e["ref"]]))
        elif "normal" in claim_conditions and special and "normal" not in seg_cond:
            local.append(Issue("condition_mismatch", claim, f"The answer presents a {', '.join(sorted(special))} "
                                                            f"value as a normal operating value.", [e["ref"]]))
        if negated(seg) != claim_neg:
            local.append(Issue("negation_mismatch", claim,
                               f"Source [{e['ref']}] and the answer differ in negation (\"not\"/\"never\").", [e["ref"]]))
        if eq.cmp and q.cmp and eq.cmp != q.cmp:
            local.append(Issue("inequality_mismatch", claim,
                               f"Source [{e['ref']}] gives {label} as a {'minimum' if eq.cmp == 'gt' else 'maximum'} "
                               f"limit; the answer states the opposite.", [e["ref"]]))
        elif eq.cmp and not q.cmp:
            local.append(Issue("inequality_missing", claim,
                               f"Source [{e['ref']}] gives {label} as a {'lower' if eq.cmp == 'gt' else 'upper'} limit; "
                               "the answer drops the limit direction.", [e["ref"]]))
        if not local:
            return            # one fully bound match is enough
        problems.extend(local)
    issues.extend(problems)


def _dedupe(issues: list[Issue]) -> list[Issue]:
    seen, out = set(), []
    for i in issues:
        key = (i.code, i.claim, i.explanation)
        if key not in seen:
            seen.add(key); out.append(i)
    return out
