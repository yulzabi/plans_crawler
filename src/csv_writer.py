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


def write_header(path: Path = CSV_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow(COLUMNS)


def append_record(record: PermitRecord, path: Path = CSV_PATH):
    if not path.exists():
        write_header(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow([getattr(record, col, "") for col in COLUMNS])


def read_all(path: Path = CSV_PATH) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def rewrite_all(rows: list[dict], path: Path = CSV_PATH):
    write_header(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        for row in rows:
            w.writerow({k: row.get(k, "") for k in COLUMNS})
