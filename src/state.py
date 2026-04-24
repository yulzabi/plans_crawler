"""Persistent state manager for crawl progress tracking."""
import json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Any

STATE_FILE = Path(__file__).parent.parent / "data" / "state.json"


@dataclass
class FileEntry:
    url: str = ""
    address: str = ""
    status: str = "pending"  # pending | processed | skipped | error
    pdfs: list[str] = field(default_factory=list)
    error: str | None = None


class StateManager:
    def __init__(self, path: Path = STATE_FILE):
        self.path = path
        self.phase: str = "collecting_files"  # collecting_files | processing_files | done
        self.last_results_page: int = 0
        self.files: dict[str, FileEntry] = {}
        self.load()

    def load(self):
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.phase = data.get("phase", "collecting_files")
            self.last_results_page = data.get("last_results_page", 0)
            for fid, fdata in data.get("building_files", {}).items():
                self.files[fid] = FileEntry(**{k: v for k, v in fdata.items() if k in FileEntry.__dataclass_fields__})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "phase": self.phase,
            "last_results_page": self.last_results_page,
            "building_files": {fid: asdict(f) for fid, f in self.files.items()},
            "stats": self.get_progress(),
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def mark_file_listed(self, file_id: str, url: str = "", address: str = ""):
        if file_id not in self.files:
            self.files[file_id] = FileEntry(url=url, address=address)
            self.save()

    def mark_file_processed(self, file_id: str, pdfs: list[str] | None = None):
        if file_id in self.files:
            self.files[file_id].status = "processed"
            if pdfs:
                self.files[file_id].pdfs = pdfs
            self.save()

    def mark_file_error(self, file_id: str, error: str):
        if file_id in self.files:
            self.files[file_id].status = "error"
            self.files[file_id].error = error
            self.save()

    def mark_file_skipped(self, file_id: str):
        if file_id in self.files:
            self.files[file_id].status = "skipped"
            self.save()

    def get_unprocessed_files(self) -> list[tuple[str, FileEntry]]:
        return [(fid, f) for fid, f in self.files.items() if f.status == "pending"]

    def get_error_files(self) -> list[tuple[str, FileEntry]]:
        return [(fid, f) for fid, f in self.files.items() if f.status == "error"]

    def get_progress(self) -> dict[str, Any]:
        total = len(self.files)
        by_status = {}
        for f in self.files.values():
            by_status[f.status] = by_status.get(f.status, 0) + 1
        return {"total": total, **by_status}
