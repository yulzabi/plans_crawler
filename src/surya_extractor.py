"""Surya OCR extractor: local fallback using Surya for Hebrew text extraction."""
import re
from io import BytesIO

from PIL import Image

# Reuse field patterns from pdf_processor
FIELD_PATTERNS = {
    "owner": r"בעל[_ ]הי?תר[:\s]*(.+)",
    "architect": r"ע?ורך[_ ]הבקשה[:\s]*(.+)",
    "gush": r"גוש[:\s]*(\d+)",
    "helka": r"חלקה[:\s]*(\d+)",
    "migrash": r"מגרש[:\s]*(\d+)",
    "permit_number": r"(?:היתר בני[הי] מספר|מספר בקשה)[:\s]*(\d+)",
    "city_plan": r"תוכנית[:\s]*([^\n]+)",
    "address": r"כתובת(?:\s*הבניה)?[:\s]*([^\n]+)",
    "date": r"בתאריך[:\s]*(\S+)",
}

FIELDS = ["owner", "architect", "address", "gush", "helka", "migrash",
          "permit_number", "date", "city_plan"]

# Module-level cache for predictors (heavy to load)
_recognition_predictor = None
_detection_predictor = None


def _get_predictors():
    global _recognition_predictor, _detection_predictor
    if _recognition_predictor is None:
        from surya.recognition import RecognitionPredictor
        from surya.detection import DetectionPredictor
        from surya.foundation import FoundationPredictor
        foundation = FoundationPredictor()
        _recognition_predictor = RecognitionPredictor(foundation)
        _detection_predictor = DetectionPredictor()
    return _recognition_predictor, _detection_predictor


class SuryaExtractor:
    def __init__(self):
        """Load Surya models (cached across instances)."""
        self.rec_predictor, self.det_predictor = _get_predictors()

    def extract(self, image_bytes: bytes) -> tuple[dict, dict]:
        """OCR image, then regex-extract fields.
        
        Returns (fields_dict, confidence_dict).
        """
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
        results = self.rec_predictor([img], det_predictor=self.det_predictor)
        
        # Combine all text lines
        text = "\n".join(line.text for line in results[0].text_lines)
        
        # Extract fields via regex
        fields = {}
        confidence = {}
        for name in FIELDS:
            pattern = FIELD_PATTERNS.get(name, "")
            if not pattern:
                fields[name] = ""
                confidence[name] = "none"
                continue
            m = re.search(pattern, text, re.MULTILINE)
            if m:
                val = m.group(1).strip()
                val = re.sub(r"[|\[\]{}><]+", "", val).strip()
                fields[name] = val
                confidence[name] = "high"
            else:
                fields[name] = ""
                confidence[name] = "none"

        return fields, confidence
