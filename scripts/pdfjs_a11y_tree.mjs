// pdfjs_a11y_tree.mjs — print the accessibility tree pdf.js (Firefox's PDF viewer)
// builds from a tagged PDF: roles, lang, alt, and the text each node resolves to.
//
// Usage: node pdfjs_a11y_tree.mjs file.pdf
// pdfjs-dist is looked up in: the current directory, $PDFJS_DIR, ~/.cache/pdf-accessibility/pdfjs
// (where setup_env.py installs it) and ~/.cache/pdfua-pdfjs.
import { createRequire } from "module";
import path from "path";
import fs from "fs";
import os from "os";
import { pathToFileURL } from "url";

const roots = [process.cwd(), process.env.PDFJS_DIR, path.join(os.homedir(), ".cache", "pdf-accessibility", "pdfjs"),
               path.join(os.homedir(), ".cache", "pdfua-pdfjs")].filter(Boolean);
let entry = null;
for (const r of roots) {
  try { entry = createRequire(path.resolve(r, "noop.js")).resolve("pdfjs-dist/legacy/build/pdf.mjs"); break; } catch {}
}
if (!entry) {
  console.error("pdfjs-dist not found: run scripts/setup_env.py (needs npm) or `npm i pdfjs-dist` in the current directory");
  process.exit(2);
}
const pdfjs = await import(pathToFileURL(entry).href);

const file = path.resolve(process.argv[2] || "");
const data = new Uint8Array(fs.readFileSync(file));
const doc = await pdfjs.getDocument({ data, verbosity: 0 }).promise;
const meta = await doc.getMetadata();
console.log(`title: ${meta.info.Title} | lang: ${meta.info.Language ?? "(none)"}`);

for (let p = 1; p <= doc.numPages; p++) {
  const page = await doc.getPage(p);
  const tc = await page.getTextContent({ includeMarkedContent: true });
  // map marked-content id -> text
  const text = {};
  let stack = [];
  for (const it of tc.items) {
    if (it.type === "beginMarkedContentProps" || it.type === "beginMarkedContent") { stack.push(it.id ?? null); continue; }
    if (it.type === "endMarkedContent") { stack.pop(); continue; }
    const id = [...stack].reverse().find((x) => x);
    if (id) text[id] = (text[id] ?? "") + it.str + (it.hasEOL ? "\n" : "");
  }
  const tree = await page.getStructTree();
  const annots = await page.getAnnotations();
  const lines = [];
  const walk = (n, d) => {
    if (n.type === "content") return text[n.id] ?? "";
    if (n.type === "annotation") return "";
    const kids = (n.children ?? []).map((c) => [c, walk(c, d + 1)]);
    const t = kids.map(([, s]) => s).join("").replace(/\s+/g, " ").trim();
    if (n.role) {
      const extra = [n.lang ? `lang=${n.lang}` : "", n.alt ? `alt=${n.alt}` : ""].filter(Boolean).join(" ");
      lines.push({ d, s: `${"  ".repeat(d)}${n.role}${extra ? ` [${extra}]` : ""}${/^(H\d|P|LI|Link|Figure)$/.test(n.role) ? ` — ${t}` : ""}` });
    }
    return t;
  };
  walk(tree, 0);
  // print in document order (pre-order): rebuild by second pass
  const out = [];
  const pre = (n, d) => {
    if (n.role) {
      const hit = lines.find((l) => l.d === d && l.s.startsWith(`${"  ".repeat(d)}${n.role}`) && !l.used);
      if (hit) { hit.used = true; out.push(hit.s); }
    }
    for (const c of n.children ?? []) if (c.type !== "content" && c.type !== "annotation") pre(c, d + 1);
  };
  pre(tree, 0);
  console.log(`--- page ${p}: ${annots.length} annotations: ${annots.map((a) => `${a.subtype}(${a.url ?? a.unsafeUrl ?? ""}) "${a.contentsObj?.str ?? ""}"`).join(", ")}`);
  console.log(out.join("\n"));
}
