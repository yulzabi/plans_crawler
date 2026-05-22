"""CSV writer for extracted permit records."""
import csv
from pathlib import Path
from src.pdf_processor import PermitRecord

CSV_PATH = Path(__file__).parent.parent / "data" / "output.csv"

COLUMNS = [
    "city", "building_file_number", "permit_number", "address", "gush", "helka",
    "migrash", "city_plan", "owner", "architect", "structural_planner",
    "pdf_path", "date",
]

CONFIDENCE_FIELDS = [
    "permit_number", "address", "gush", "helka", "migrash",
    "city_plan", "owner", "architect", "date",
]


def _columns_with_confidence():
    """Return columns list with confidence suffix columns appended."""
    return COLUMNS + [f"{f}_confidence" for f in CONFIDENCE_FIELDS]


def write_header(path: Path = CSV_PATH, with_confidence: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = _columns_with_confidence() if with_confidence else COLUMNS
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(cols)


def append_record(record: PermitRecord, path: Path = CSV_PATH, confidence: dict = None):
    if not path.exists():
        write_header(path, with_confidence=confidence is not None)
    row = [getattr(record, col, "") for col in COLUMNS]
    if confidence:
        row += [confidence.get(f, "") for f in CONFIDENCE_FIELDS]
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(row)


def read_all(path: Path = CSV_PATH) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def rewrite_all(rows: list[dict], path: Path = CSV_PATH):
    # Detect if any row has confidence data
    has_confidence = any(f"{f}_confidence" in rows[0] for f in CONFIDENCE_FIELDS) if rows else False
    cols = _columns_with_confidence() if has_confidence else COLUMNS
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        dw = csv.DictWriter(f, fieldnames=cols)
        for row in rows:
            dw.writerow({k: row.get(k, "") for k in cols})


def count_low_confidence(row: dict) -> int:
    """Count fields with 'low' or 'none' confidence. Used for --fix prioritization."""
    return sum(1 for f in CONFIDENCE_FIELDS if row.get(f"{f}_confidence") in ("low", "none"))
