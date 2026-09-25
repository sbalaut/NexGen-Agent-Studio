// Reading documents in the browser and cutting them into passages ("chunks").
// Text, Markdown, PDF (pdf.js) and Word .docx (mammoth). PDF/DOCX libraries load only when needed.

export const TAG_RE = /\b(?:\d{1,3}-)?[A-Z]{1,4}-\d{2,5}[A-Z]?\b/g;
export const tagsIn = (text: string) => [...new Set(text.match(TAG_RE) || [])].sort();

export type Block = { text: string; section: string; location: string; kind: "text" | "table_row" | "heading" };
export type Piece = { section: string; location: string; kind: "text" | "table_row"; text: string; tags: string[] };

const MAX_CHARS = 1100;

function looksLikeHeading(line: string): boolean {
  const t = line.trim();
  if (!t || t.length > 90 || /[.,;]$/.test(t)) return false;
  if (/^\d+(\.\d+)*\.?\s+[A-Z]/.test(t) && t.split(/\s+/).length <= 10) return true;       // "3.2 Startup"
  const letters = t.replace(/[^A-Za-z]/g, "");
  return letters.length >= 4 && letters === letters.toUpperCase() && t.split(/\s+/).length <= 8;   // "STARTUP PROCEDURE"
}

const cells = (row: string) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

/** Markdown or plain text → blocks. Markdown tables become one block per row: "Header: value | Header: value". */
export function parseText(text: string, markdown: boolean): Block[] {
  const out: Block[] = [];
  let section = "(no heading)";
  let para: string[] = [];
  let paraStart = 0;
  let header: string[] | null = null;
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const flush = () => {
    if (para.length) out.push({ text: para.join(" ").replace(/\s+/g, " ").trim(), section, location: `line ${paraStart}`, kind: "text" });
    para = [];
  };
  lines.forEach((raw, i) => {
    const line = raw.trim();
    const md = markdown && line.match(/^#{1,6}\s+(.*)$/);
    if (md || (!markdown && looksLikeHeading(line) && !para.length)) {
      flush(); header = null;
      section = (md ? md[1] : line).replace(/\*\*/g, "").trim() || section;
      out.push({ text: section, section, location: `line ${i + 1}`, kind: "heading" });
      return;
    }
    if (markdown && line.startsWith("|") && line.endsWith("|")) {
      flush();
      const c = cells(line);
      if (c.every((x) => /^:?-{2,}:?$/.test(x))) return;          // separator row
      if (!header) { header = c; return; }
      const rowNo = out.filter((b) => b.kind === "table_row" && b.section === section).length + 1;
      out.push({ text: c.map((v, k) => `${header![k] || "Column " + (k + 1)}: ${v}`).join(" | "), section,
        location: `line ${i + 1}, table row ${rowNo}`, kind: "table_row" });
      return;
    }
    header = null;
    if (!line) { flush(); return; }
    if (!para.length) paraStart = i + 1;
    para.push(line.replace(/^[-*+]\s+/, "• "));
  });
  flush();
  return out.filter((b) => b.text);
}

/** Blocks → passages: paragraphs of one section are packed up to ~1100 characters; table rows stay single. */
export function chunkBlocks(blocks: Block[]): Piece[] {
  const out: Piece[] = [];
  let buf: Block[] = [];
  const flush = () => {
    if (!buf.length) return;
    const text = buf.map((b) => b.text).join("\n");
    out.push({ section: buf[0].section, location: buf[0].location, kind: "text", text, tags: tagsIn(text + " " + buf[0].section) });
    buf = [];
  };
  for (const b of blocks) {
    if (b.kind === "heading") { flush(); continue; }
    if (b.kind === "table_row") { flush(); out.push({ section: b.section, location: b.location, kind: "table_row", text: b.text, tags: tagsIn(b.text) }); continue; }
    if (buf.length && (buf[0].section !== b.section || buf.reduce((n, x) => n + x.text.length, 0) + b.text.length > MAX_CHARS)) flush();
    if (b.text.length > MAX_CHARS * 1.6) {              // very long paragraph: split on sentences
      flush();
      let part = "";
      for (const s of b.text.split(/(?<=[.!?])\s+/)) {
        if ((part + " " + s).length > MAX_CHARS && part) { out.push({ section: b.section, location: b.location, kind: "text", text: part.trim(), tags: tagsIn(part) }); part = ""; }
        part += " " + s;
      }
      if (part.trim()) out.push({ section: b.section, location: b.location, kind: "text", text: part.trim(), tags: tagsIn(part) });
      continue;
    }
    buf.push(b);
  }
  flush();
  return out;
}

// ------------------------------------------------------------------ PDF
async function parsePdf(data: ArrayBuffer): Promise<{ blocks: Block[]; warning?: string }> {
  const pdfjs: any = await import("pdfjs-dist");
  const workerUrl = (await import("pdfjs-dist/build/pdf.worker.min.mjs?url")).default;
  pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;
  const doc = await pdfjs.getDocument({ data, isEvalSupported: false }).promise;
  const blocks: Block[] = [];
  let section = "(no heading)";
  let chars = 0;
  for (let p = 1; p <= doc.numPages; p++) {
    const page = await doc.getPage(p);
    const tc = await page.getTextContent();
    // group text items into lines by their y coordinate
    const lines: { y: number; h: number; parts: { x: number; s: string }[] }[] = [];
    for (const it of tc.items as any[]) {
      if (!("str" in it) || !it.str.trim()) continue;
      const y = Math.round(it.transform[5]), h = Math.abs(it.transform[3]) || it.height || 10;
      let line = lines.find((l) => Math.abs(l.y - y) <= 2);
      if (!line) { line = { y, h, parts: [] }; lines.push(line); }
      line.parts.push({ x: it.transform[4], s: it.str });
      line.h = Math.max(line.h, h);
    }
    lines.sort((a, b) => b.y - a.y);
    // body text size = the line height that carries the most characters on this page
    const weight = new Map<number, number>();
    for (const l of lines) { const h = Math.round(l.h); weight.set(h, (weight.get(h) || 0) + l.parts.reduce((n, x) => n + x.s.length, 0)); }
    const body = [...weight.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || 10;
    let para: string[] = [];
    let prevY: number | null = null;
    const flush = () => { if (para.length) blocks.push({ text: para.join(" ").replace(/\s+/g, " ").trim(), section, location: `page ${p}`, kind: "text" }); para = []; };
    for (const l of lines) {
      const text = l.parts.sort((a, b) => a.x - b.x).map((x) => x.s).join(" ").replace(/\s+/g, " ").trim();
      chars += text.length;
      if ((l.h > body * 1.15 && text.length < 100) || looksLikeHeading(text)) {
        flush(); section = text; blocks.push({ text, section, location: `page ${p}`, kind: "heading" });
      } else {
        if (prevY !== null && prevY - l.y > l.h * 1.9) flush();          // blank space → new paragraph
        para.push(text);
      }
      prevY = l.y;
    }
    flush();
  }
  const warning = chars < 40 * doc.numPages ? "Very little text was found — this may be a scanned PDF. OCR is not included." : undefined;
  return { blocks, warning };
}

// ------------------------------------------------------------------ DOCX
async function parseDocx(data: ArrayBuffer): Promise<Block[]> {
  const mammoth: any = await import("mammoth");
  const { value: html } = await mammoth.convertToHtml({ arrayBuffer: data });
  const dom = new DOMParser().parseFromString(html, "text/html");
  const blocks: Block[] = [];
  let section = "(no heading)";
  let n = 0;
  for (const el of Array.from(dom.body.children)) {
    n++;
    const tag = el.tagName.toLowerCase();
    const text = (el.textContent || "").replace(/\s+/g, " ").trim();
    if (!text) continue;
    if (/^h[1-6]$/.test(tag)) { section = text; blocks.push({ text, section, location: `paragraph ${n}`, kind: "heading" }); continue; }
    if (tag === "table") {
      const rows = Array.from(el.querySelectorAll("tr")).map((tr) => Array.from(tr.querySelectorAll("th,td")).map((c) => (c.textContent || "").trim()));
      const [head, ...rest] = rows;
      rest.forEach((r, k) => blocks.push({ text: r.map((v, i) => `${head?.[i] || "Column " + (i + 1)}: ${v}`).join(" | "), section,
        location: `paragraph ${n}, table row ${k + 1}`, kind: "table_row" }));
      continue;
    }
    if (tag === "ul" || tag === "ol") {
      for (const li of Array.from(el.querySelectorAll("li"))) blocks.push({ text: "• " + (li.textContent || "").trim(), section, location: `paragraph ${n}`, kind: "text" });
      continue;
    }
    blocks.push({ text, section, location: `paragraph ${n}`, kind: "text" });
  }
  return blocks;
}

export const ACCEPT = ".txt,.md,.markdown,.pdf,.docx";

export async function readFile(file: File): Promise<{ pieces: Piece[]; warning?: string }> {
  const name = file.name.toLowerCase();
  if (file.size > 40 * 1024 * 1024) throw new Error(`${file.name} is larger than 40 MB.`);
  let blocks: Block[];
  let warning: string | undefined;
  if (name.endsWith(".pdf")) ({ blocks, warning } = await parsePdf(await file.arrayBuffer()));
  else if (name.endsWith(".docx")) blocks = await parseDocx(await file.arrayBuffer());
  else if (/\.(md|markdown|txt)$/.test(name)) blocks = parseText(await file.text(), !name.endsWith(".txt"));
  else throw new Error(`${file.name}: only .txt, .md, .pdf and .docx files are supported.`);
  const pieces = chunkBlocks(blocks);
  if (!pieces.length) throw new Error(`${file.name}: no text could be read.`);
  return { pieces, warning };
}
