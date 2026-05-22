"""PDF processor: OCR and field extraction from building permit PDFs."""
import re
from pathlib import Path
from io import BytesIO
from dataclasses import dataclass

import fitz
import pytesseract
from PIL import Image

# Increase PIL limit for large scanned plans
Image.MAX_IMAGE_PIXELS = 1_500_000_000  # ~1.5B pixels, covers large A0 scans


@dataclass
class PermitRecord:
    city: str = ""
    building_file_number: str = ""
    permit_number: str = ""
    address: str = ""
    gush: str = ""
    helka: str = ""
    migrash: str = ""
    city_plan: str = ""
    owner: str = ""
    architect: str = ""
    structural_planner: str = ""
    pdf_path: str = ""
    date: str = ""


def find_permit_pages(pdf_path: str, max_width_mm: float = 300) -> list[int]:
    """Return page indices that are likely permit forms (not plan drawings).
    
    Filters out pages wider than max_width_mm (A0/A1 plan drawings).
    A4 = 210mm, A3 = 297mm, so 300mm threshold keeps both.
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []
    pages = []
    for i in range(len(doc)):
        width_mm = doc[i].rect.width * 25.4 / 72
        if width_mm < max_width_mm:
            pages.append(i)
    doc.close()
    return pages


def render_page(pdf_path: str, page_num: int, dpi: int = 150) -> bytes:
    """Render a single PDF page to PNG bytes."""
    doc = fitz.open(pdf_path)
    pix = doc[page_num].get_pixmap(dpi=dpi)
    png_bytes = pix.tobytes("png")
    doc.close()
    return png_bytes


# Regex patterns — tolerant of OCR errors (missing/garbled first char)
FIELD_PATTERNS = {
    "owner": r"בעל[_ ]הי?תר[:\s]*(.+)",
    "architect": r"ע?ורך[_ ]הבקשה[:\s]*(.+)",
    "structural_planner": r"מתכנן[_ ]שלד[:\s]*(.+)",
    "gush": r"גוש[:\s]*(\d+)",
    "helka": r"חלקה[:\s]*(\d+)",
    "migrash": r"מגרש[:\s]*(\d+)",
    "permit_number": r"היתר בני[הי] מספר[:\s]*(\S+)",
    "building_file": r"תיק בני[ין][:\s]*(\d+)",
    "city_plan": r"תוכנית[:\s]*([^\n]+)",
    "address": r"כתובת[:\s]*([^\n]+)",
}


def ocr_page(pdf_path: str, page_num: int = 0, dpi: int = 300) -> str:
    """OCR a single page of a PDF, return extracted text."""
    doc = fitz.open(pdf_path)
    if page_num >= len(doc):
        doc.close()
        return ""
    page = doc[page_num]
    pix = page.get_pixmap(dpi=dpi)
    img = Image.open(BytesIO(pix.tobytes("png")))
    doc.close()

    # Skip oversized images (large plan drawings, not permit forms)
    if img.width * img.height > 200_000_000:
        # Retry at lower DPI
        doc = fitz.open(pdf_path)
        pix = doc[page_num].get_pixmap(dpi=150)
        img = Image.open(BytesIO(pix.tobytes("png")))
        doc.close()

    # Preprocess: grayscale + contrast boost
    img = img.convert("L")
    from PIL import ImageEnhance
    img = ImageEnhance.Contrast(img).enhance(1.5)

    return pytesseract.image_to_string(img, lang="heb+eng", config="--psm 6")


def is_building_permit(text: str) -> bool:
    """Check if OCR text is from a building permit document."""
    indicators = ["היתר בני", "טופס 3", "תקנה 18", "היתר זה"]
    return any(ind in text for ind in indicators)


def extract_field(text: str, pattern: str) -> str:
    """Extract a field value using regex, return empty string if not found."""
    m = re.search(pattern, text, re.MULTILINE)
    if m:
        val = m.group(1).strip()
        # Clean up common OCR artifacts
        val = re.sub(r"[|\[\]{}><]+", "", val).strip()
        val = re.sub(r"\s{2,}", " ", val)
        return val
    return ""


def extract_fields(text: str) -> dict:
    """Extract all known fields from OCR text."""
    return {name: extract_field(text, pat) for name, pat in FIELD_PATTERNS.items()}


def ocr_name_area(pdf_path: str, dpi: int = 400) -> str:
    """OCR the name/address area (top 18-30% of page 1) for better field extraction."""
    doc = fitz.open(pdf_path)
    page = doc[0]
    pix = page.get_pixmap(dpi=dpi)
    img = Image.open(BytesIO(pix.tobytes("png")))
    doc.close()

    # Reduce DPI for oversized pages
    if img.width * img.height > 200_000_000:
        doc = fitz.open(pdf_path)
        pix = doc[0].get_pixmap(dpi=200)
        img = Image.open(BytesIO(pix.tobytes("png")))
        doc.close()

    w, h = img.size
    crop = img.crop((0, int(h * 0.18), w, int(h * 0.30))).convert("L")
    from PIL import ImageEnhance
    crop = ImageEnhance.Contrast(crop).enhance(1.5)
    return pytesseract.image_to_string(crop, lang="heb+eng", config="--psm 6")


def process_pdf(pdf_path: str, mode: str = "cloud") -> tuple[PermitRecord | None, dict | None]:
    """Process a PDF: extract permit fields using cloud or local OCR.
    
    mode: 'cloud' (Bedrock), 'local' (Surya), or 'tesseract' (legacy)
    Returns (PermitRecord, confidence_dict) or (None, None).
    """
    if mode == "tesseract":
        return _process_pdf_tesseract(pdf_path), None

    # Find A4/A3 permit pages (skip plan drawings)
    pages = find_permit_pages(pdf_path)
    if not pages:
        return None, None

    # Render first permit page
    img = render_page(pdf_path, pages[0])

    if mode == "cloud":
        try:
            from src.bedrock_extractor import BedrockExtractor
            extractor = BedrockExtractor()
            fields, confidence = extractor.extract(img)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Bedrock failed ({e}), falling back to tesseract")
            return _process_pdf_tesseract(pdf_path), None
    elif mode == "local":
        try:
            from src.surya_extractor import SuryaExtractor
            extractor = SuryaExtractor()
            fields, confidence = extractor.extract(img)
        except ImportError:
            import logging
            logging.getLogger(__name__).warning("Surya not installed, falling back to tesseract")
            return _process_pdf_tesseract(pdf_path), None
    else:
        raise ValueError(f"Unknown mode: {mode}")

    record = PermitRecord(
        building_file_number=fields.get("building_file", ""),
        permit_number=fields.get("permit_number", ""),
        address=fields.get("address", ""),
        gush=fields.get("gush", ""),
        helka=fields.get("helka", ""),
        migrash=fields.get("migrash", ""),
        city_plan=fields.get("city_plan", ""),
        owner=fields.get("owner", ""),
        architect=fields.get("architect", ""),
        structural_planner=fields.get("structural_planner", ""),
        pdf_path=pdf_path,
        date=fields.get("date", ""),
    )
    return record, confidence


def _process_pdf_tesseract(pdf_path: str) -> PermitRecord | None:
    """Legacy Tesseract-based extraction (fallback)."""
    text = ocr_page(pdf_path, page_num=0)
    if not text or not is_building_permit(text):
        return None

    name_text = ocr_name_area(pdf_path)
    combined = text + "\n" + name_text
    fields = extract_fields(combined)

    return PermitRecord(
        building_file_number=fields.get("building_file", ""),
        permit_number=fields.get("permit_number", ""),
        address=fields.get("address", ""),
        gush=fields.get("gush", ""),
        helka=fields.get("helka", ""),
        migrash=fields.get("migrash", ""),
        city_plan=fields.get("city_plan", ""),
        owner=fields.get("owner", ""),
        architect=fields.get("architect", ""),
        structural_planner=fields.get("structural_planner", ""),
        pdf_path=pdf_path,
    )


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "/Users/yulazari/Downloads/225205.PDF"
    mode = sys.argv[2] if len(sys.argv) > 2 else "cloud"
    print(f"Processing: {path} (mode={mode})")
    record, confidence = process_pdf(path, mode=mode)
    if record:
        print("\nExtracted fields:")
        for k, v in record.__dict__.items():
            if v and k != "pdf_path":
                conf = confidence.get(k, "") if confidence else ""
                print(f"  {k}: {v}" + (f" [{conf}]" if conf else ""))
    else:
        print("Not a building permit or extraction failed")
