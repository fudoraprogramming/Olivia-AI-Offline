"""
olivia_pdf_processor.py — PDF extraction for Olivia v8.

PyMuPDF (fitz) preferred — it's faster and handles huge PDFs page-by-page
without loading the whole document into RAM.  Falls back to pypdf if
PyMuPDF isn't installed.

Smart split preserves sentence and paragraph boundaries so chunks read
naturally and embed better.
"""

import os

try:
    import fitz  # PyMuPDF
    PYMUPDF = True
except ImportError:
    PYMUPDF = False

try:
    from pypdf import PdfReader
    PYPDF = True
except ImportError:
    PYPDF = False


class PDFProcessor:
    """Handles PDFs from 1 page to 25,000+ pages, page-by-page."""

    def __init__(self, chunk_size=1200, overlap=150):
        self.chunk_size = chunk_size
        self.overlap = overlap

    def info(self, path):
        """Quick metadata without extracting full text."""
        if not os.path.exists(path):
            return None
        out = {"size_mb": round(os.path.getsize(path) / (1024 * 1024), 2)}
        if PYMUPDF:
            try:
                doc = fitz.open(path)
                out["pages"] = len(doc)
                meta = doc.metadata or {}
                out["title"] = meta.get("title", "") or ""
                out["author"] = meta.get("author", "") or ""
                doc.close()
            except Exception as e:
                out["error"] = str(e)
        elif PYPDF:
            try:
                r = PdfReader(path)
                out["pages"] = len(r.pages)
                out["title"] = ""
            except Exception as e:
                out["error"] = str(e)
        return out

    def extract_text(self, path, progress_cb=None):
        """Extract text from every page. progress_cb(page_idx, total_pages)."""
        if not os.path.exists(path):
            return ""
        if PYMUPDF:
            return self._extract_pymupdf(path, progress_cb)
        if PYPDF:
            return self._extract_pypdf(path, progress_cb)
        raise RuntimeError("No PDF backend installed. pip install pymupdf")

    def _extract_pymupdf(self, path, progress_cb):
        out = []
        try:
            doc = fitz.open(path)
            total = len(doc)
            for i in range(total):
                try:
                    page = doc[i]
                    text = page.get_text() or ""
                    if text.strip():
                        out.append(text)
                except Exception:
                    pass
                if progress_cb and (i % 20 == 0 or i == total - 1):
                    progress_cb(i + 1, total)
            doc.close()
        except Exception as e:
            print(f"PyMuPDF error on {path}: {e}")
        return "\n\n".join(out)

    def _extract_pypdf(self, path, progress_cb):
        out = []
        try:
            r = PdfReader(path)
            total = len(r.pages)
            for i, page in enumerate(r.pages):
                try:
                    text = page.extract_text() or ""
                    if text.strip():
                        out.append(text)
                except Exception:
                    pass
                if progress_cb and (i % 20 == 0 or i == total - 1):
                    progress_cb(i + 1, total)
        except Exception as e:
            print(f"pypdf error on {path}: {e}")
        return "\n\n".join(out)

    def smart_split(self, text):
        """Split into overlapping chunks, breaking on sentence/paragraph
        boundaries when possible."""
        text = (text or "").strip()
        if not text:
            return []
        if len(text) <= self.chunk_size:
            return [text]
        chunks = []
        start = 0
        n = len(text)
        while start < n:
            end = min(start + self.chunk_size, n)
            chunk = text[start:end]
            if end < n:
                # prefer paragraph break, then sentence, then space
                last_para = chunk.rfind("\n\n")
                last_period = chunk.rfind(". ")
                last_newline = chunk.rfind("\n")
                last_space = chunk.rfind(" ")
                # only honor a break if it's past the midpoint
                cuts = [c for c in (last_para, last_period, last_newline, last_space)
                        if c > self.chunk_size // 2]
                if cuts:
                    cut = max(cuts)
                    chunk = text[start:start + cut + 1]
                    end = start + cut + 1
            chunks.append(chunk.strip())
            if end >= n:
                break
            start = max(end - self.overlap, 0)
        return [c for c in chunks if len(c) > 30]

    def scan_folder(self, folder):
        out = []
        if not os.path.isdir(folder):
            return out
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(".pdf"):
                    out.append(os.path.join(root, f))
        out.sort()
        return out


if __name__ == "__main__":
    p = PDFProcessor()
    print(f"PyMuPDF: {PYMUPDF}   pypdf: {PYPDF}")
    sample = "This is a test. " * 200
    chunks = p.smart_split(sample)
    print(f"sample {len(sample)} chars -> {len(chunks)} chunks")
