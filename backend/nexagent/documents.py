"""Document extraction into preview blocks.

Runs in a separate process (`python -m nexagent.documents <file>`) with a timeout
so a malformed PDF cannot hang or crash the service. Output is JSON on stdout.

Supported: text PDFs, DOCX, TXT, Markdown. Scanned PDFs are detected and reported
(OCR is deferred). Tables are preserved row by row with their header names so
equipment/value/condition stay bound together in one block.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

PARSER_VERSION = "nx-parse-1.0"
SECTION_TYPES = ("process_description", "startup", "shutdown", "interlock",
                 "troubleshooting", "equipment_spec", "other")

# Order matters: "emergency shutdown" is an interlock topic, not a routine shutdown.
_SECTION_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("interlock", ("interlock", "trip", "esd", "emergency shutdown", "safety instrumented", "sis ")),
    ("startup", ("startup", "start-up", "start up", "commissioning", "pre-commissioning")),
    ("shutdown", ("shutdown", "shut-down", "shut down", "decommission")),
    ("troubleshooting", ("troubleshoot", "trouble shooting", "problem", "fault", "abnormal", "diagnos")),
    ("equipment_spec", ("specification", "equipment data", "data sheet", "datasheet", "design data",
                        "design basis", "equipment list", "rating", "operating condition", "operating parameter",
                        "normal operating", "operating data")),
    ("process_description", ("process description", "process flow", "description", "overview",
                             "introduction", "process chemistry", "flow scheme")),
]

TAG_RE = re.compile(r"\b(?:\d{1,3}-)?[A-Z]{1,4}-\d{2,5}[A-Z]?\b")


def classify_section(heading: str) -> str:
    h = " " + heading.lower() + " "
    for label, words in _SECTION_RULES:
        if any(w in h for w in words):
            return label
    return "other"


def equipment_tags(text: str) -> list[str]:
    return sorted(set(TAG_RE.findall(text)))


def _block(heading: str, kind: str, text: str, location: str) -> dict:
    return {"heading": heading or "(no heading)", "section_type": classify_section(heading),
            "kind": kind, "text": text.strip(), "location": location, "equipment_tags": equipment_tags(text)}


def _table_rows(header: list[str], rows: list[list[str]], heading: str, location: str) -> list[dict]:
    out = []
    for i, row in enumerate(rows, start=1):
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        if header and len(header) == len(cells):
            text = " | ".join(f"{h.strip()}: {c}" for h, c in zip(header, cells))
        else:
            text = " | ".join(cells)
        out.append(_block(heading, "table_row", text, f"{location}, table row {i}"))
    return out


def parse_markdown(text: str, is_markdown: bool = True) -> dict:
    blocks: list[dict] = []
    heading, para, table = "", [], []
    line_no = 0

    def flush_para():
        nonlocal para
        if para:
            blocks.append(_block(heading, "text", " ".join(para), f"line {line_no}"))
            para = []

    def flush_table():
        nonlocal table
        if table:
            rows = [[c for c in r.strip().strip("|").split("|")] for r in table]
            rows = [r for r in rows if not all(re.fullmatch(r"\s*:?-{2,}:?\s*", c) for c in r)]
            if rows:
                blocks.extend(_table_rows(rows[0], rows[1:], heading, f"line {line_no}"))
            table = []

    for line_no, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        m = re.match(r"^(#{1,6})\s+(.*)$", s) if is_markdown else None
        plain_heading = (not is_markdown and s and len(s) < 80 and not s.endswith((".", ",", ";", ":"))
                         and (s.isupper() or re.match(r"^\d+(\.\d+)*\.?\s+\S", s)))
        if m or plain_heading:
            flush_para(); flush_table()
            heading = (m.group(2) if m else s).strip()
            continue
        if s.startswith("|") and s.count("|") >= 2:
            flush_para(); table.append(s); continue
        if not s:
            flush_para(); flush_table(); continue
        flush_table()
        para.append(s.lstrip("-*• ").strip() if s[:2] in ("- ", "* ", "• ") else s)
    flush_para(); flush_table()
    return {"status": "preview_ready", "blocks": blocks}


def parse_docx(path: Path) -> dict:
    import docx  # python-docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    blocks, heading, para_no, table_no = [], "", 0, 0
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            para_no += 1
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name or "").lower() if p.style is not None else ""
            if style.startswith("heading") or style == "title":
                heading = text
                continue
            blocks.append(_block(heading, "text", text, f"paragraph {para_no}"))
        elif tag == "tbl":
            table_no += 1
            t = Table(child, d)
            rows = [[c.text for c in r.cells] for r in t.rows]
            if rows:
                blocks.extend(_table_rows(rows[0], rows[1:], heading, f"table {table_no}"))
    return {"status": "preview_ready", "blocks": blocks}


def parse_pdf(path: Path) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [(i, (p.extract_text() or "")) for i, p in enumerate(reader.pages, start=1)]
    total = sum(len(t.strip()) for _, t in pages)
    if not pages or total / max(1, len(pages)) < 40:
        return {"status": "scanned_ocr_deferred", "blocks": [],
                "message": "This PDF has little or no text layer (it looks scanned). OCR is not included in this "
                           "release; upload a text PDF, DOCX or TXT version instead."}
    blocks, heading = [], ""
    for page_no, text in pages:
        para: list[str] = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                if para:
                    blocks.append(_block(heading, "text", " ".join(para), f"page {page_no}")); para = []
                continue
            looks_heading = (len(s) < 70 and not s.endswith((".", ",", ";")) and
                             (s.isupper() or re.match(r"^\d+(\.\d+)*\.?\s+[A-Z]", s)) and len(s.split()) <= 9)
            if looks_heading:
                if para:
                    blocks.append(_block(heading, "text", " ".join(para), f"page {page_no}")); para = []
                heading = s
                continue
            para.append(s)
            if s.endswith(".") and sum(len(x) for x in para) > 500:
                blocks.append(_block(heading, "text", " ".join(para), f"page {page_no}")); para = []
        if para:
            blocks.append(_block(heading, "text", " ".join(para), f"page {page_no}"))
    return {"status": "preview_ready", "blocks": blocks}


def parse_file(path: Path) -> dict:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return parse_pdf(path)
    if ext == ".docx":
        return parse_docx(path)
    if ext in (".md", ".markdown"):
        return parse_markdown(path.read_text(encoding="utf-8", errors="replace"), True)
    if ext == ".txt":
        return parse_markdown(path.read_text(encoding="utf-8", errors="replace"), False)
    return {"status": "failed", "blocks": [], "message": f"Unsupported file type {ext}"}


def parse_isolated(path: Path, timeout_s: int = 180) -> dict:
    try:
        proc = subprocess.run([sys.executable, "-m", "nexagent.documents", str(path)],
                              capture_output=True, text=True, timeout=timeout_s,
                              cwd=str(Path(__file__).resolve().parent.parent))
    except subprocess.TimeoutExpired:
        return {"status": "failed", "blocks": [], "message": f"Parsing took longer than {timeout_s} s and was stopped."}
    if proc.returncode != 0:
        return {"status": "failed", "blocks": [], "message": "The parser could not read this file: "
                + (proc.stderr.strip().splitlines() or ["unknown error"])[-1][:300]}
    return json.loads(proc.stdout)


def chunk_blocks(blocks: list[dict], max_chars: int = 900) -> list[dict]:
    """Heading-aware chunks. Table rows are never merged; text merges within one heading."""
    chunks: list[dict] = []
    cur: dict | None = None
    for b in blocks:
        if not b.get("include", 1):
            continue
        if b["kind"] == "table_row":
            cur = None
            chunks.append(dict(b))
            continue
        if cur and cur["heading"] == b["heading"] and cur["section_type"] == b["section_type"] \
                and len(cur["text"]) + len(b["text"]) < max_chars:
            cur["text"] += "\n" + b["text"]
            cur["equipment_tags"] = sorted(set(cur["equipment_tags"]) | set(b["equipment_tags"]))
            if cur["location"] != b["location"]:
                cur["location"] = cur["location"].split(" – ")[0] + " – " + b["location"]
        else:
            cur = dict(b)
            chunks.append(cur)
    return chunks


if __name__ == "__main__":  # isolated parser entry point
    result = parse_file(Path(sys.argv[1]))
    result["parser_version"] = PARSER_VERSION
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
