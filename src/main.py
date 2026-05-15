"""Main orchestrator for the Ness Ziona building plans crawler."""
import asyncio
import re
import signal
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from PIL import Image

Image.MAX_IMAGE_PIXELS = 1_500_000_000  # ~1.5B pixels, covers large A0 scans

from src.state import StateManager
from src.browser import BrowserController, BlockedError
from src.pdf_processor import process_pdf
from src.csv_writer import append_record, write_header, CSV_PATH

DATA_DIR = Path(__file__).parent.parent / "data"
PDF_DIR = DATA_DIR / "pdfs"
OCR_WORKERS = 4

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
    pdf_path, file_id, address, date = args
    try:
        from src.pdf_processor import process_pdf as _process
        record = _process(pdf_path)
        if record:
            record.building_file_number = file_id
            record.date = date
            if not record.address:
                record.address = address
            return record.__dict__
    except Exception as e:
        return {"_error": str(e), "pdf_path": pdf_path}
    return None


async def crawl(retry_errors: bool = False):
    global _shutdown
    state = StateManager()
    bc = BrowserController()

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

        if not CSV_PATH.exists():
            write_header()

        print(f"\n🚀 Processing: {remaining} files ({OCR_WORKERS} OCR workers)")

        # OCR queue: feed downloaded PDFs here, workers pick them up
        ocr_queue = asyncio.Queue()
        ocr_done = asyncio.Event()
        loop = asyncio.get_event_loop()
        pool = ProcessPoolExecutor(max_workers=OCR_WORKERS)
        csv_lock = asyncio.Lock()
        ocr_count = [0]

        # Enqueue already-downloaded files from previous interrupted run
        for file_id, entry in already_downloaded:
            for i, pdf in enumerate(entry.pdfs):
                date = entry.doc_meta[i]["date"] if i < len(entry.doc_meta) else ""
                await ocr_queue.put((pdf, file_id, entry.address, date))

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
                            append_record(record)
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
                        dest = PDF_DIR / dirname / filename
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        await bc.download_pdf(doc_info["url"], str(dest))
                        pdf_paths.append(str(dest))
                        dm = {"date": doc_info.get("date", ""), "entity_number": doc_info.get("entity_number", "")}
                        meta.append(dm)
                        # Feed to OCR queue immediately
                        await ocr_queue.put((str(dest), file_id, entry.address, dm["date"]))
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
        print(f"CSV: {CSV_PATH} ({ocr_count[0]} records written)")

    finally:
        if bc:
            await bc.close()


def show_status():
    state = StateManager()
    p = state.get_progress()
    print(f"Phase: {state.phase}")
    for k, v in p.items():
        print(f"  {k}: {v}")
    if CSV_PATH.exists():
        lines = sum(1 for _ in open(CSV_PATH)) - 1
        print(f"CSV records: {lines}")


def fix_missing():
    """Open PDFs with missing owner/architect and prompt user to fill in."""
    import subprocess
    from src.csv_writer import read_all, rewrite_all, COLUMNS

    rows = read_all()
    if not rows:
        print("No CSV data found.")
        return

    to_fix = [(i, r) for i, r in enumerate(rows) if not r.get("owner") or not r.get("architect")]
    print(f"Found {len(to_fix)} records with missing owner/architect out of {len(rows)} total.\n")

    fixed = 0
    for idx, row in to_fix:
        pdf = row.get("pdf_path", "")
        addr = row.get("address", "?")
        print(f"[{fixed+1}/{len(to_fix)}] File {row.get('building_file_number','?')}: {addr}")
        print(f"  Current: owner={row.get('owner','')!r}, architect={row.get('architect','')!r}")

        if pdf and Path(pdf).exists():
            subprocess.Popen(["open", pdf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"  📄 Opened: {pdf}")
        else:
            print(f"  ⚠ PDF not found: {pdf}")

        print("  Fill in missing fields (Enter to skip, 'q' to quit):")
        for field in COLUMNS:
            current = row.get(field, "")
            if current and field not in ("owner", "architect", "structural_planner"):
                continue
            if field in ("pdf_path", "date", "building_file_number"):
                continue
            val = input(f"    {field} [{current}]: ").strip()
            if val == "q":
                rewrite_all(rows)
                print(f"\n✓ Saved. Fixed {fixed} records.")
                return
            if val:
                rows[idx][field] = val
        fixed += 1
        print()

    rewrite_all(rows)
    print(f"✓ Done. Fixed {fixed} records. CSV updated.")


async def update():
    """Re-fetch the file list and only process new entries."""
    state = StateManager()
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


if __name__ == "__main__":
    if "--status" in sys.argv:
        show_status()
    elif "--fix" in sys.argv:
        fix_missing()
    elif "--update" in sys.argv:
        asyncio.run(update())
    elif "--retry-errors" in sys.argv:
        asyncio.run(crawl(retry_errors=True))
    else:
        asyncio.run(crawl())
