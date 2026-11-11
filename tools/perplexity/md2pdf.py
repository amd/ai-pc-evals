# Copyright(C) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""Markdown -> styled HTML (marked + mermaid in the browser) -> PDF via headless Chrome.

usage: python md2pdf.py <in.md> <out.pdf>
"""
import html
import json
import os
import subprocess
import sys
from pathlib import Path

CHROME = os.environ.get("CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
src, out = Path(sys.argv[1]), Path(sys.argv[2])
cover = "" if "--no-cover" in sys.argv else '<div class="cover">Internal evaluation document. Contains answer-key values.</div>'
md = src.read_text(encoding="utf-8")
title = next((l[2:].strip() for l in md.splitlines() if l.startswith("# ")), src.stem)

page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
@page {{ size: Letter; margin: 18mm 16mm 18mm 16mm; }}
body {{ font: 10.5pt/1.5 "Segoe UI", system-ui, sans-serif; color: #1c1b19; }}
h1 {{ font-size: 22pt; margin: 0 0 6pt; letter-spacing: -.01em; }}
h2 {{ font-size: 15pt; margin: 20pt 0 6pt; padding-bottom: 3pt; border-bottom: 1px solid #ddd8ce; break-after: avoid; }}
h3 {{ font-size: 12.5pt; margin: 14pt 0 4pt; break-after: avoid; }}
h4 {{ font-size: 11pt; margin: 12pt 0 4pt; color: #2f5fb3; break-after: avoid; }}
h2#the-16-use-cases ~ h4 {{ break-before: auto; }}
p, li {{ orphans: 3; widows: 3; }}
table {{ border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 9pt; break-inside: auto; }}
tr {{ break-inside: avoid; }}
th, td {{ border: 1px solid #ddd8ce; padding: 3pt 5pt; text-align: left; vertical-align: top; }}
th {{ background: #f3f1ec; }}
code {{ font: 8.8pt Consolas, monospace; background: #f3f1ec; padding: 0 2pt; border-radius: 2px; }}
pre {{ background: #f6f4ef; padding: 6pt 8pt; border-radius: 4px; overflow-wrap: anywhere; white-space: pre-wrap; font-size: 8.5pt; break-inside: avoid; }}
pre code {{ background: none; padding: 0; }}
blockquote {{ margin: 6pt 0; padding: 4pt 10pt; border-left: 3px solid #2f5fb3; background: #f5f8fd; }}
hr {{ border: 0; border-top: 1px solid #ddd8ce; margin: 14pt 0; }}
.flow {{ display: flex; flex-direction: column; align-items: center; margin: 8pt 0 12pt; break-inside: avoid; }}
.step {{ border: 1px solid #c9d6ef; background: #f2f6fd; border-radius: 6px; padding: 4pt 10pt; font-size: 9.5pt; text-align: center; min-width: 60%; }}
.arrow {{ color: #2f5fb3; font-size: 11pt; line-height: 1.1; }}
a {{ color: #2f5fb3; text-decoration: none; }}
.cover {{ color: #6d6a63; font-size: 9pt; margin-bottom: 10pt; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"></script>
</head><body>{cover}<main id="doc"></main>
<script>
const md = {json.dumps(md)};
const slug = t => t.toLowerCase().replace(/<[^>]+>/g,'').replace(/[^\\w\\s-]/g,'').trim().replace(/\\s+/g,'-');
const renderer = new marked.Renderer();
renderer.heading = (text, level) => `<h${{level}} id="${{slug(text)}}">${{text}}</h${{level}}>`;
// Flowcharts become a plain box-and-arrow strip: mermaid does not render reliably in headless print.
const flow = code => {{ const seen = new Set(); const steps = [];
  for (const m of code.matchAll(/\\[([^\\]]+)\\]/g)) {{ const t = m[1].replace(/<br\\/?>/g, ' — '); if (!seen.has(t)) {{ seen.add(t); steps.push(t); }} }}
  return '<div class="flow">' + steps.map(t => `<div class="step">${{t}}</div>`).join('<div class="arrow">↓</div>') + '</div>'; }};
renderer.code = (code, lang) => lang === 'mermaid' ? flow(code)
  : `<pre><code>${{code.replace(/&/g,'&amp;').replace(/</g,'&lt;')}}</code></pre>`;
document.getElementById('doc').innerHTML = marked.parse(md, {{ renderer, gfm: true }});

</script></body></html>"""
html_path = out.with_suffix(".html")
html_path.write_text(page, encoding="utf-8")
if not Path(CHROME).is_file():
    sys.exit(f"ERROR: Chrome not found at {CHROME}; set CHROME_PATH to chrome.exe (or msedge.exe)")
subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                "--virtual-time-budget=15000", f"--print-to-pdf={out}", html_path.resolve().as_uri()],
               check=True, capture_output=True, timeout=120)
print(out, out.stat().st_size, "bytes")
