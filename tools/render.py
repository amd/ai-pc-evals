"""Render a deliverable to one PNG per slide or page, as a person would see it.

usage: python tools/render.py <file> <out-dir> [--backend auto|office|libreoffice] [--dpi 110]

Everything goes through PDF: Office (PowerShell COM, Windows) or LibreOffice converts
pptx/docx/xlsx, Chrome prints html, and pypdfium2 (PDFium) rasterises the pages. Images pass through.
With --backend auto, a Windows PC without Office (its COM classes not registered) falls back to
LibreOffice when soffice is found.
The source is copied first, so a deliverable is never opened in place.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

OFFICE = {".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xlsm", ".xls"}
HTML = {".html", ".htm"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
CONVERT_TIMEOUT_S = 180
CHROME = os.environ.get("CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
MAX_PAGES = 40

# Opens a copy read-only and invisible, exports PDF, closes it. PowerPoint and Word are
# single-instance, so the app may be the user's own: quit it only if nothing else is open.
_PS_EXPORT = r"""
$ErrorActionPreference = 'Stop'
$kind, $src, $pdf = $args
switch ($kind) {
  'ppt' { $app = New-Object -ComObject PowerPoint.Application
          try { $doc = $app.Presentations.Open($src, -1, 0, 0); $doc.SaveAs($pdf, 32); $doc.Close() }
          finally { if ($app.Presentations.Count -eq 0) { $app.Quit() } } }
  'doc' { $app = New-Object -ComObject Word.Application; $app.DisplayAlerts = 0
          try { $doc = $app.Documents.Add($src, $false, 0, $false); $doc.ExportAsFixedFormat($pdf, 17); $doc.Close(0) }
          finally { if ($app.Documents.Count -eq 0) { $app.Quit() } } }
  'xls' { $app = New-Object -ComObject Excel.Application; $app.DisplayAlerts = $false
          try { $doc = $app.Workbooks.Open($src, 0, $true); $doc.ExportAsFixedFormat(0, $pdf); $doc.Close($false) }
          finally { if ($app.Workbooks.Count -eq 0) { $app.Quit() } } }
}
"""


class RenderError(RuntimeError):
    pass


class OfficeMissing(RenderError):
    """The Office export failed because Office isn't installed or its COM classes aren't registered."""


RENDERER_HINT = "install Microsoft Office or LibreOffice, or pass --backend libreoffice"
# What PowerShell prints when New-Object -ComObject finds no registered Office class.
_OFFICE_MISSING = re.compile(
    r"REGDB_E_CLASSNOTREG|80040154|Class not registered|Cannot load COM type", re.I)
# Set once an auto-mode Office export finds Office missing, so later renders go straight to LibreOffice.
_office_missing = False


def office_kind(suffix: str) -> str:
    return {".pptx": "ppt", ".ppt": "ppt", ".docx": "doc", ".doc": "doc"}.get(suffix, "xls")


def soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        if shutil.which(name):
            return shutil.which(name)
    for p in (r"C:\Program Files\LibreOffice\program\soffice.exe", "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if Path(p).is_file():
            return p
    return None


def pick_backend(backend: str) -> str:
    if backend != "auto":
        return backend
    if sys.platform == "win32" and not _office_missing:
        return "office"
    if soffice():
        return "libreoffice"
    raise RenderError(f"no renderer: {RENDERER_HINT}")


def _run(cmd: list[str], what: str) -> None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=CONVERT_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"{what} did not finish in {CONVERT_TIMEOUT_S}s") from e
    except FileNotFoundError as e:
        raise RenderError(f"{what}: {cmd[0]} not found") from e
    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip()
        msg = f"{what} exited {out.returncode}: {detail[-500:]}"
        if _OFFICE_MISSING.search(detail):
            raise OfficeMissing(f"{msg}\nMicrosoft Office is not installed or not registered")
        raise RenderError(msg)


def _office_export(src: Path, pdf: Path, work: Path) -> None:
    script = work / "export.ps1"
    script.write_text(_PS_EXPORT, encoding="utf-8")
    _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
          str(script), office_kind(src.suffix.lower()), str(src.resolve()), str(pdf.resolve())],
         f"Office export of {src.name}")


def _libreoffice_export(src: Path, work: Path) -> None:
    exe = soffice()
    if not exe:
        raise RenderError(f"LibreOffice (soffice) not found: {RENDERER_HINT}")
    _run([exe, "--headless", f"-env:UserInstallation={(work / 'lo-profile').resolve().as_uri()}",
          "--convert-to", "pdf", "--outdir", str(work), str(src)], f"LibreOffice export of {src.name}")


def _convert_office(src: Path, pdf: Path, work: Path, backend: str) -> None:
    """Office or LibreOffice; auto mode falls back to LibreOffice when Office isn't installed."""
    global _office_missing
    if pick_backend(backend) == "libreoffice":
        _libreoffice_export(src, work)
        return
    try:
        _office_export(src, pdf, work)
    except OfficeMissing as e:
        if backend != "auto":
            raise RenderError(f"{e}: {RENDERER_HINT}") from e
        _office_missing = True
        if not soffice():
            raise RenderError(f"{e}, and LibreOffice (soffice) was not found: {RENDERER_HINT}") from e
        print("note: Microsoft Office is not available; rendering with LibreOffice", file=sys.stderr)
        _libreoffice_export(src, work)


def to_pdf(src: Path, work: Path, backend: str) -> Path:
    """A PDF of *src* inside *work*; *src* is already a private copy."""
    suffix = src.suffix.lower()
    pdf = work / (src.stem + ".pdf")
    if suffix == ".pdf":
        return src
    if suffix in HTML:
        if not Path(CHROME).is_file() and not shutil.which(CHROME):
            raise RenderError(f"Chrome not found at {CHROME}; set CHROME_PATH")
        _run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
              f"--print-to-pdf={pdf}", src.resolve().as_uri()], "Chrome print-to-pdf")
    elif suffix in OFFICE:
        if suffix in (".pptx", ".docx", ".xlsx", ".xlsm") and not zipfile.is_zipfile(src):
            raise RenderError(f"not a real {suffix} file (text saved under a {suffix} name?)")
        _convert_office(src, pdf, work, backend)
    else:
        raise RenderError(f"can't render {suffix} files")
    if not pdf.is_file():
        raise RenderError(f"no PDF was produced for {src.name}")
    return pdf


def pdf_to_pngs(pdf: Path, out_dir: Path, stem: str, dpi: int) -> list[Path]:
    import pypdfium2 as pdfium

    out = []
    doc = pdfium.PdfDocument(pdf)
    try:
        if len(doc) > MAX_PAGES:
            print(f"warning: {pdf.name} has {len(doc)} pages; rendering the first {MAX_PAGES}", file=sys.stderr)
        for i in range(min(len(doc), MAX_PAGES)):
            png = out_dir / f"{stem}-p{i + 1:02d}.png"
            doc[i].render(scale=dpi / 72).to_pil().convert("RGB").save(png)
            out.append(png)
    finally:
        doc.close()
    return out


def render(path: Path, out_dir: Path, backend: str = "auto", dpi: int = 110) -> list[Path]:
    """PNG files, one per slide or page, written to *out_dir* in order."""
    path = Path(path)
    if not path.is_file():
        raise RenderError(f"{path} does not exist")
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in IMAGES:
        dest = out_dir / f"{path.stem}-p01{suffix}"
        shutil.copyfile(path, dest)
        return [dest]
    with tempfile.TemporaryDirectory(prefix="kw-render-") as tmp:
        work = Path(tmp)
        copy = work / ("src" + suffix)
        shutil.copyfile(path, copy)
        pdf = to_pdf(copy, work, backend)
        return pdf_to_pngs(pdf, out_dir, path.stem, dpi)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--backend", choices=("auto", "office", "libreoffice"), default="auto")
    ap.add_argument("--dpi", type=int, default=110)
    args = ap.parse_args(argv)
    try:
        for p in render(args.file, args.out_dir, args.backend, args.dpi):
            print(p)
    except RenderError as e:
        sys.exit(f"ERROR: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
