"""Main orchestrator for the building plans crawler. Supports multiple cities."""
import asyncio
import csv as csv_mod
import re
import signal
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from PIL import Image

Image.MAX_IMAGE_PIXELS = 1_500_000_000  # ~1.5B pixels, covers large A0 scans

from src.state import StateManager
from src.pdf_processor import process_pdf
from src.csv_writer import append_record, write_header, CSV_PATH, COLUMNS, read_all, rewrite_all

PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = PROJECT_DIR / "data"
CITIES_FILE = PROJECT_DIR / "cities.csv"
OCR_WORKERS = 4


def _get_city() -> str:
    for i, arg in enumerate(sys.argv):
        if arg == "--city" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return "nes"


def _city_csv(city: str) -> Path:
    d = DATA_DIR / city
    d.mkdir(parents=True, exist_ok=True)
    return d / "output.csv"


def _city_pdf_dir(city: str) -> Path:
    d = DATA_DIR / city / "pdfs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _city_state(city: str) -> Path:
    d = DATA_DIR / city
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


def _load_cities() -> list[dict]:
    if not CITIES_FILE.exists():
        return []
    with open(CITIES_FILE, encoding="utf-8") as f:
        return [{"subdomain": row[0], "name": row[1]} for row in csv_mod.reader(f) if row and not row[0].startswith("#")]


# Keep backward compat — default paths for single-city mode
PDF_DIR = DATA_DIR / "pdfs"

_shutdown = False


def _handle_sigint(sig, frame):
    global _shutdown
    if _shutdown:
        print("\n⚡ Force quit")
        sys.exit(1)
    _shutdown = True
    print("\n🛑 Shutting down gracefully... (Ctrl+C again to force)")


signal.signal(signal.SIGINT, _handle_sigint)


def sanitize_dirname(address: str) -> str:
    name = re.sub(r"[/\\:*?\"<>|]", "_", address.strip())
    return re.sub(r"\s+", "_", name)


def _ocr_one(args: tuple) -> dict | None:
    """Worker function for parallel OCR. Runs in a subprocess."""
    from PIL import Image as _Img
    _Img.MAX_IMAGE_PIXELS = 1_500_000_000
    pdf_path, file_id, address, date, city_name, mode = args if len(args) == 6 else (*args, "local")
    if mode == "skip":
        return None
    try:
        from src.pdf_processor import process_pdf as _process
        result = _process(pdf_path, mode=mode)
        record, confidence = result if isinstance(result, tuple) else (result, None)
        if record:
            record.city = city_name
            record.building_file_number = file_id
            record.date = date
            if not record.address:
                record.address = address
            out = record.__dict__
            if confidence:
                out["_confidence"] = confidence
            return out
    except Exception as e:
        return {"_error": str(e), "pdf_path": pdf_path}
    return None


async def crawl(retry_errors: bool = False, ocr_mode: str = "local"):
    global _shutdown
    city = _get_city()
    city_csv = _city_csv(city)
    city_pdfs = _city_pdf_dir(city)
    state = StateManager(path=_city_state(city))
    from src.browser import BrowserController, BlockedError
    bc = BrowserController(subdomain=city)

    print(f"🏙 City: {city} | CSV: {city_csv}")

    try:
        await bc.launch()

        # Phase 1: Collect file list
        if state.phase == "collecting_files":
            print("📋 Collecting building file list...")
            await bc.navigate_to_search()
            await bc.perform_search()
            results = await bc.get_results_list()
            for r in results:
                state.mark_file_listed(r["file_id"], url=r["url"], address=r["address"])
            state.phase = "processing_files"
            state.save()
            print(f"✓ Collected {len(results)} building files")

        if retry_errors:
            for fid, entry in state.get_error_files():
                entry.status = "pending"
                entry.error = None
            state.save()

        # Phase 2: Download + OCR in parallel
        unprocessed = state.get_unprocessed_files()
        already_downloaded = state.get_downloaded_files()
        total = len(state.files)
        remaining = len(unprocessed) + len(already_downloaded)

        if remaining == 0 and not retry_errors:
            print("✓ Already complete. Use --retry-errors to reprocess failures.")
            return

        if not city_csv.exists():
            write_header(city_csv)

        print(f"\n🚀 Processing: {remaining} files ({OCR_WORKERS} OCR workers)")

        # OCR queue: feed downloaded PDFs here, workers pick them up
        ocr_queue = asyncio.Queue()
        ocr_done = asyncio.Event()
        loop = asyncio.get_event_loop()
        pool = ProcessPoolExecutor(max_workers=OCR_WORKERS)
        csv_lock = asyncio.Lock()
        ocr_count = [0]

        # Load existing CSV to know which PDFs already have results
        existing_pdfs = {r.get("pdf_path") for r in read_all(city_csv) if r.get("pdf_path")}

        async def queue_if_needed(pdf_path, file_id, address, date):
            """Only queue for OCR if not already in CSV."""
            if pdf_path not in existing_pdfs:
                await ocr_queue.put((pdf_path, file_id, address, date, city, ocr_mode))

        # Enqueue already-downloaded files from previous interrupted run
        if already_downloaded:
            queued = 0
            for file_id, entry in already_downloaded:
                for i, pdf in enumerate(entry.pdfs):
                    date = entry.doc_meta[i]["date"] if i < len(entry.doc_meta) else ""
                    if pdf not in existing_pdfs:
                        await ocr_queue.put((pdf, file_id, entry.address, date, city, ocr_mode))
                        queued += 1
            if queued:
                print(f"  ↻ {queued} PDFs from previous run need OCR")

        async def ocr_consumer():
            """Consume OCR queue, run in process pool, write to CSV."""
            from src.pdf_processor import PermitRecord
            while True:
                try:
                    item = await asyncio.wait_for(ocr_queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    if ocr_done.is_set() and ocr_queue.empty():
                        break
                    continue

                try:
                    result = await loop.run_in_executor(pool, _ocr_one, item)
                    if result and "_error" not in result:
                        record = PermitRecord(**{k: v for k, v in result.items() if k in PermitRecord.__dataclass_fields__})
                        async with csv_lock:
                            append_record(record, city_csv)
                            existing_pdfs.add(record.pdf_path)
                        ocr_count[0] += 1
                except Exception as e:
                    print(f"  ⚠ OCR: {e}")
                finally:
                    ocr_queue.task_done()

        # Start OCR consumers
        consumers = [asyncio.create_task(ocr_consumer()) for _ in range(OCR_WORKERS)]

        # Download loop (sequential browser navigation)
        done = total - remaining
        for file_id, entry in unprocessed:
            if _shutdown:
                print(f"\n🛑 Stopped. Run again to resume.")
                break

            try:
                done += 1
                print(f"[{done}/{total}] {file_id}: {entry.address}", end=" ", flush=True)
                await bc.navigate_to_file(entry.url)
                docs = await bc.get_pdf_links()

                permits = []
                for d in docs:
                    if "היתר בניה" not in d.get("doc_type", ""):
                        continue
                    try:
                        year = int(d.get("date", "").split("/")[-1])
                        if year < 2000:
                            continue
                    except (ValueError, IndexError):
                        pass
                    permits.append(d)

                if not permits:
                    state.mark_file_skipped(file_id)
                    print("⏭")
                    continue

                pdf_paths = []
                meta = []
                for doc_info in permits:
                    try:
                        dirname = sanitize_dirname(entry.address) or f"file_{file_id}"
                        nf = doc_info["url"].split("Name_File=")[1].split("&")[0]
                        filename = f"permit_{doc_info.get('entity_number') or nf}.pdf"
                        dest = city_pdfs / dirname / filename
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        await bc.download_pdf(doc_info["url"], str(dest))
                        pdf_paths.append(str(dest))
                        dm = {"date": doc_info.get("date", ""), "entity_number": doc_info.get("entity_number", "")}
                        meta.append(dm)
                        # Feed to OCR queue immediately
                        await ocr_queue.put((str(dest), file_id, entry.address, dm["date"], city, ocr_mode))
                    except Exception as e:
                        print(f"⚠dl:{e}", end=" ")

                if pdf_paths:
                    state.mark_file_downloaded(file_id, pdf_paths, meta)
                    print(f"✓ {len(pdf_paths)} PDFs (OCR queue: ~{ocr_queue.qsize()})")
                else:
                    state.mark_file_skipped(file_id)
                    print("⏭")

            except BlockedError as e:
                print(f"\n{e}")
                print("💾 Saving progress...")
                state.save()
                break
            except Exception as e:
                print(f"✗ {e}")
                state.mark_file_error(file_id, str(e))

        # Signal OCR consumers to finish
        ocr_done.set()
        await bc.close()
        bc = None

        # Wait for OCR to drain
        if not ocr_queue.empty():
            print(f"\n⏳ Waiting for OCR to finish ({ocr_queue.qsize()} remaining)...")
        for c in consumers:
            await c
        pool.shutdown()

        # Mark all downloaded as processed
        for file_id, entry in state.get_downloaded_files():
            state.mark_file_processed(file_id, entry.pdfs)

        if not _shutdown:
            state.phase = "done"
        state.save()

        progress = state.get_progress()
        print(f"\n{'='*50}")
        print(f"Done! {progress}")
        print(f"CSV: {city_csv} ({ocr_count[0]} records written)")

    finally:
        if bc:
            await bc.close()


def show_status():
    city = _get_city()
    state = StateManager(path=_city_state(city))
    p = state.get_progress()
    print(f"City: {city}")
    print(f"Phase: {state.phase}")
    for k, v in p.items():
        print(f"  {k}: {v}")
    csv_path = _city_csv(city)
    if csv_path.exists():
        lines = sum(1 for _ in open(csv_path)) - 1
        print(f"CSV records: {lines}")


def _is_valid_field(val: str) -> bool:
    """Check if a field value is real data (not empty, not OCR garbage)."""
    if not val or len(val.strip()) < 2:
        return False
    val = val.strip()
    if re.match(r'^[\d\s./\-]+$', val):
        return False  # pure numbers aren't names
    heb = re.findall(r'[\u0590-\u05FF]', val)
    return len(heb) / max(len(val.replace(' ', '')), 1) >= 0.25


def _is_complete(row: dict) -> bool:
    """A row is complete if owner AND architect have valid data."""
    return _is_valid_field(row.get("owner", "")) and _is_valid_field(row.get("architect", ""))


def cleanup():
    """Delete PDFs for records where all key data is extracted."""
    city = _get_city()
    rows = read_all(_city_csv(city))
    if not rows:
        print("No CSV data found.")
        return

    deleted = 0
    kept = 0
    for row in rows:
        if not _is_complete(row):
            kept += 1
            continue
        pdf = row.get("pdf_path", "")
        if pdf and Path(pdf).exists():
            Path(pdf).unlink()
            deleted += 1
            # Remove empty parent dir
            parent = Path(pdf).parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()

    print(f"✓ Deleted {deleted} PDFs (complete records). {kept} kept (incomplete).")


async def fix_missing():
    """Interactive fix: open PDFs with missing data, prompt user, delete when complete."""
    import subprocess
    city = _get_city()
    city_csv = _city_csv(city)

    rows = read_all(city_csv)
    if not rows:
        print("No CSV data found.")
        return

    to_fix = [(i, r) for i, r in enumerate(rows) if not _is_complete(r)]
    if not to_fix:
        print("✓ All records are complete!")
        return

    # Check which PDFs need re-downloading
    needs_download = [(i, r) for i, r in to_fix if not r.get("pdf_path") or not Path(r["pdf_path"]).exists()]

    bc = None
    if needs_download:
        print(f"📥 {len(needs_download)} PDFs need re-downloading. Launching browser...")
        from src.browser import BrowserController
        bc = BrowserController()
        await bc.launch()

        for idx, row in needs_download:
            file_id = row.get("building_file_number", "")
            if not file_id:
                continue
            try:
                url = f"/BuildingArchiveDetails?OrgEntityNumber={file_id}&pageId=8575&DefinementEntityType=10&BuildingNum={file_id}"
                await bc.navigate_to_file(url)
                docs = await bc.get_pdf_links()
                permits = [d for d in docs if "היתר בניה" in d.get("doc_type", "")]
                if permits:
                    d = permits[0]
                    dirname = sanitize_dirname(row.get("address", "")) or f"file_{file_id}"
                    nf = d["url"].split("Name_File=")[1].split("&")[0]
                    dest = _city_pdf_dir(_get_city()) / dirname / f"permit_{nf}.pdf"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    await bc.download_pdf(d["url"], str(dest))
                    rows[idx]["pdf_path"] = str(dest)
                    print(f"  ✓ Downloaded: {row.get('address','?')}")
            except Exception as e:
                print(f"  ⚠ Could not download for file {file_id}: {e}")

        await bc.close()

    print(f"\n{'='*50}")
    print(f"📝 Manual Fix Mode — {len(to_fix)} records to review")
    print(f"   Commands: Enter=skip field | s=skip record | q=save & quit")
    print(f"{'='*50}\n")

    FIELDS_TO_FIX = ["owner", "architect"]
    fixed = 0

    try:
        for idx, row in to_fix:
            addr = row.get("address", "?")
            file_id = row.get("building_file_number", "?")
            pdf = row.get("pdf_path", "")

            print(f"┌─ [{fixed+1}/{len(to_fix)}] תיק {file_id} — {addr}")
            print(f"│  תאריך: {row.get('date', '?')}  |  גוש: {row.get('gush', '?')}  |  חלקה: {row.get('helka', '?')}")
            print(f"│  בעל היתר: {row.get('owner', '') or '❌ חסר'}")
            print(f"│  אדריכל: {row.get('architect', '') or '❌ חסר'}")

            # Ask before opening PDF
            has_pdf = pdf and Path(pdf).exists()
            try:
                action = input(f"│  [Enter=פתח PDF | s=דלג | q=שמור וצא]: ").strip().lower()
            except EOFError:
                action = ""

            if action == "q":
                rewrite_all(rows, city_csv)
                print(f"\n✓ Saved. Fixed {fixed} records.")
                return
            if action == "s":
                print(f"└─ ⏭ דילוג\n")
                continue

            # Open PDF
            if has_pdf:
                subprocess.Popen(["open", pdf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"│  📄 PDF נפתח")
            else:
                print(f"│  ⚠ אין PDF זמין")

            print(f"│")

            # Collect inputs
            skip_record = False
            edits = {}
            for field in FIELDS_TO_FIX:
                current = row.get(field, "")
                if _is_valid_field(current):
                    continue
                label = "בעל היתר" if field == "owner" else "אדריכל"
                try:
                    val = input(f"│  ✏️  {label} [{current or '—'}]: ").strip()
                except EOFError:
                    break
                if val == "q":
                    rewrite_all(rows, city_csv)
                    print(f"\n✓ Saved. Fixed {fixed} records.")
                    return
                if val == "s":
                    skip_record = True
                    break
                if val:
                    edits[field] = val

            if skip_record:
                print(f"└─ ⏭ דילוג\n")
                continue

            if not edits:
                print(f"└─ ⏭ ללא שינוי\n")
                continue

            # Show summary and confirm
            print(f"│")
            print(f"│  📋 סיכום שינויים:")
            for field, val in edits.items():
                label = "בעל היתר" if field == "owner" else "אדריכל"
                print(f"│     {label}: {val}")

            try:
                confirm = input(f"│  💾 לשמור? (y/n) [y]: ").strip().lower()
            except EOFError:
                confirm = "y"

            if confirm in ("", "y", "כ"):
                for field, val in edits.items():
                    rows[idx][field] = val
                fixed += 1

                # Delete PDF if now complete
                if _is_complete(rows[idx]) and pdf and Path(pdf).exists():
                    Path(pdf).unlink()
                    parent = Path(pdf).parent
                    if parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()
                    print(f"└─ ✓ נשמר + PDF נמחק\n")
                else:
                    print(f"└─ ✓ נשמר\n")
            else:
                print(f"└─ ✗ בוטל\n")

    except KeyboardInterrupt:
        print(f"\n\n🛑 נעצר.")

    rewrite_all(rows, city_csv)
    print(f"\n{'='*50}")
    print(f"✓ סיום. תוקנו {fixed} רשומות. CSV עודכן.")


async def update():
    """Re-fetch the file list and only process new entries."""
    state = StateManager()
    from src.browser import BrowserController
    bc = BrowserController()

    try:
        await bc.launch()
        print("🔄 Fetching current file list from portal...")
        await bc.navigate_to_search()
        await bc.perform_search()
        results = await bc.get_results_list()

        existing_ids = set(state.files.keys())
        new_entries = [r for r in results if r["file_id"] not in existing_ids]

        if not new_entries:
            print(f"✓ No new files. Archive still has {len(results)} files.")
            return

        print(f"Found {len(new_entries)} new files (was {len(existing_ids)}, now {len(results)})")
        for r in new_entries:
            state.mark_file_listed(r["file_id"], url=r["url"], address=r["address"])
        state.phase = "downloading"
        state.save()
        print("Run 'python -m src.main' to process them.")

    finally:
        await bc.close()


def reocr():
    """Re-OCR PDFs where owner or architect is missing. Updates CSV in place. Parallel."""
    from concurrent.futures import ProcessPoolExecutor

    city = _get_city()
    city_csv = _city_csv(city)
    rows = read_all(city_csv)
    if not rows:
        print("No CSV data found.")
        return

    to_fix = [(i, r) for i, r in enumerate(rows) if not r.get("owner") or not r.get("architect")]
    work = [(i, r["pdf_path"]) for i, r in to_fix if r.get("pdf_path") and Path(r["pdf_path"]).exists()]
    print(f"🔍 Re-OCR: {len(work)} PDFs with missing data ({OCR_WORKERS} workers)")

    with ProcessPoolExecutor(max_workers=OCR_WORKERS) as pool:
        futures = {pool.submit(_reocr_one, p): i for i, (_, p) in enumerate(work)}
        results = [None] * len(work)
        done_count = 0
        from concurrent.futures import as_completed
        for future in as_completed(futures):
            i = futures[future]
            results[i] = future.result()
            done_count += 1
            if done_count % 10 == 0 or done_count == len(work):
                print(f"  {done_count}/{len(work)} ({done_count*100//len(work)}%)", flush=True)

    fixed = 0
    for (idx, _), result in zip(work, results):
        if not result:
            continue
        updated = False
        for field in ("owner", "architect", "structural_planner", "gush", "helka", "migrash", "city_plan", "permit_number"):
            new_val = result.get(field, "")
            if new_val and not rows[idx].get(field):
                rows[idx][field] = new_val
                updated = True
        if updated:
            fixed += 1

    rewrite_all(rows, city_csv)
    print(f"✓ Updated {fixed} records. CSV saved.")


def _reocr_one(pdf_path: str) -> dict | None:
    from PIL import Image as _Img
    _Img.MAX_IMAGE_PIXELS = 1_500_000_000
    try:
        from src.pdf_processor import process_pdf as _process
        result = _process(pdf_path, mode="cloud")
        record, _ = result if isinstance(result, tuple) else (result, None)
        return record.__dict__ if record else None
    except Exception:
        return None


def list_cities():
    cities = _load_cities()
    print(f"📋 {len(cities)} cities configured:\n")
    for c in cities:
        state_path = _city_state(c["subdomain"])
        status = ""
        if state_path.exists():
            s = StateManager(path=state_path)
            p = s.get_progress()
            status = f" — {p.get('total',0)} files, {p.get('processed',0)} done"
        print(f"  {c['subdomain']:12} {c['name']}{status}")


def merge_csvs():
    """Merge all city CSVs into one unified file."""
    cities = _load_cities()
    all_rows = []
    for c in cities:
        csv_path = _city_csv(c["subdomain"])
        if csv_path.exists():
            rows = read_all(csv_path)
            # Ensure city column is set
            for r in rows:
                if not r.get("city"):
                    r["city"] = c["name"]
            all_rows.extend(rows)
            print(f"  {c['subdomain']}: {len(rows)} records")

    if not all_rows:
        print("No data found.")
        return

    merged_path = DATA_DIR / "all_cities.csv"
    rewrite_all(all_rows, merged_path)
    print(f"\n✓ Merged {len(all_rows)} records → {merged_path}")


if __name__ == "__main__":
    # Parse --mode flag (cloud|local|skip|tesseract)
    ocr_mode = "local"
    model_id = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    for i, arg in enumerate(sys.argv):
        if arg == "--mode" and i + 1 < len(sys.argv):
            ocr_mode = sys.argv[i + 1]
        if arg == "--model" and i + 1 < len(sys.argv):
            model_id = sys.argv[i + 1]

    if "--status" in sys.argv:
        show_status()
    elif "--fix" in sys.argv:
        asyncio.run(fix_missing())
    elif "--cleanup" in sys.argv:
        cleanup()
    elif "--reocr" in sys.argv:
        reocr()
    elif "--batch" in sys.argv:
        # Batch mode: prepare all PDFs and submit to Bedrock batch inference
        from src.batch_processor import run_batch
        from src.pdf_processor import find_permit_pages
        city = _get_city()
        city_csv = _city_csv(city)
        city_pdfs = _city_pdf_dir(city)
        pdf_paths = [str(p) for p in Path(city_pdfs).rglob("*.pdf")]

        if not pdf_paths:
            print(f"❌ No PDFs found in {city_pdfs}")
            print("  Run 'python -m src.main' first to crawl and download PDFs.")
            sys.exit(1)

        # Filter to PDFs with permit pages
        valid_pdfs = [p for p in pdf_paths if find_permit_pages(p)]
        print(f"📋 Found {len(pdf_paths)} PDFs, {len(valid_pdfs)} have permit pages (A4)")
        if not valid_pdfs:
            print("❌ No PDFs with A4 permit pages found.")
            sys.exit(1)

        batch_idx = sys.argv.index("--batch")
        role_arn = sys.argv[batch_idx + 1] if batch_idx + 1 < len(sys.argv) else None
        if not role_arn or role_arn.startswith("--"):
            print("Usage: --batch <IAM_ROLE_ARN>")
            print(f"  Example: --batch arn:aws:iam::086541416368:role/BedrockBatchInferenceRole")
            sys.exit(1)

        cost_est = len(valid_pdfs) * 0.006
        print(f"💰 Estimated cost: ~${cost_est:.2f} ({len(valid_pdfs)} pages × $0.006)")
        confirm = input("Submit batch job? [y/N] ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            sys.exit(0)

        results = run_batch(valid_pdfs, role_arn, model_id=model_id)
        existing = {r.get("pdf_path") for r in read_all(city_csv)} if city_csv.exists() else set()
        written = 0
        for pdf_path, record, confidence in results:
            if record.pdf_path in existing:
                continue
            record.city = city
            append_record(record, path=city_csv, confidence=confidence)
            written += 1
        print(f"✓ Wrote {written} new records to {city_csv} ({len(results) - written} skipped as duplicates)")
    elif "--update" in sys.argv:
        asyncio.run(update())
    elif "--cities" in sys.argv:
        list_cities()
    elif "--merge" in sys.argv:
        merge_csvs()
    elif "--retry-errors" in sys.argv:
        asyncio.run(crawl(retry_errors=True, ocr_mode=ocr_mode))
    else:
        # Check for unknown flags
        known = {"--mode", "--model", "--city", "--status", "--fix", "--cleanup",
                 "--reocr", "--batch", "--update", "--cities", "--merge", "--retry-errors"}
        unknown = [a for a in sys.argv[1:] if a.startswith("--") and a not in known]
        if unknown:
            print(f"❌ Unknown flag: {unknown[0]}")
            print("\nUsage: python -m src.main [OPTIONS]")
            print("  --mode cloud|local|skip|tesseract  OCR mode (default: local)")
            print("  --model MODEL_ID                   Bedrock model ID")
            print("  --batch ROLE_ARN                   Batch inference via S3")
            print("  --reocr                            Re-OCR PDFs with missing data")
            print("  --fix                              Interactive fix for incomplete records")
            print("  --cleanup                          Delete PDFs for complete records")
            print("  --status                           Show crawl status")
            print("  --retry-errors                     Retry failed downloads")
            print("  --cities                           List configured cities")
            print("  --merge                            Merge all city CSVs")
            sys.exit(1)
        asyncio.run(crawl(ocr_mode=ocr_mode))
