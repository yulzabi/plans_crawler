"""Main orchestrator for the Ness Ziona building plans crawler."""
import asyncio
import re
import signal
import sys
from pathlib import Path
from PIL import Image

Image.MAX_IMAGE_PIXELS = 500_000_000

from src.state import StateManager
from src.browser import BrowserController
from src.pdf_processor import process_pdf
from src.csv_writer import append_record, write_header, CSV_PATH

DATA_DIR = Path(__file__).parent.parent / "data"
PDF_DIR = DATA_DIR / "pdfs"

# Graceful shutdown flag
_shutdown = False


def _handle_sigint(sig, frame):
    global _shutdown
    if _shutdown:
        print("\n⚡ Force quit")
        sys.exit(1)
    _shutdown = True
    print("\n🛑 Shutting down gracefully after current file... (Ctrl+C again to force)")


signal.signal(signal.SIGINT, _handle_sigint)


def sanitize_dirname(address: str) -> str:
    name = re.sub(r"[/\\:*?\"<>|]", "_", address.strip())
    return re.sub(r"\s+", "_", name)


async def crawl(retry_errors: bool = False):
    global _shutdown
    state = StateManager()
    bc = BrowserController()

    try:
        await bc.launch()

        # Phase 1: Collect all building file links
        if state.phase == "collecting_files":
            print("📋 Phase 1: Collecting building file list...")
            await bc.navigate_to_search()
            await bc.perform_search()
            results = await bc.get_results_list()
            for r in results:
                state.mark_file_listed(r["file_id"], url=r["url"], address=r["address"])
            state.phase = "processing_files"
            state.save()
            print(f"✓ Collected {len(results)} building files")

        # Reset error files if retrying
        if retry_errors:
            for fid, entry in state.get_error_files():
                entry.status = "pending"
                entry.error = None
            state.save()
            print(f"♻ Reset {len(state.get_error_files())} error files for retry")

        # Phase 2: Process each building file
        if state.phase in ("processing_files", "done"):
            if state.phase == "done" and not retry_errors:
                print("✓ Already complete. Use --retry-errors to reprocess failures.")
                return
            state.phase = "processing_files"

            if not CSV_PATH.exists():
                write_header()

            unprocessed = state.get_unprocessed_files()
            total = len(state.files)
            done = total - len(unprocessed)
            print(f"\n📄 Phase 2: Processing files ({done}/{total} done, {len(unprocessed)} remaining)")

            for file_id, entry in unprocessed:
                if _shutdown:
                    print(f"\n🛑 Stopped. Progress saved. Run again to resume.")
                    return

                try:
                    print(f"\n[{done+1}/{total}] File {file_id}: {entry.address}")
                    await bc.navigate_to_file(entry.url)
                    docs = await bc.get_pdf_links()

                    # Filter for building permits from 2000 onwards
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
                        done += 1
                        continue

                    pdf_paths = []
                    for doc_info in permits:
                        try:
                            dirname = sanitize_dirname(entry.address) or f"file_{file_id}"
                            nf = doc_info["url"].split("Name_File=")[1].split("&")[0]
                            filename = f"permit_{doc_info.get('entity_number') or nf}.pdf"
                            dest = PDF_DIR / dirname / filename
                            dest.parent.mkdir(parents=True, exist_ok=True)

                            print(f"  ⬇ {filename}...", end=" ", flush=True)
                            await bc.download_pdf(doc_info["url"], str(dest))
                            pdf_paths.append(str(dest))

                            record = process_pdf(str(dest))
                            if record:
                                record.building_file_number = file_id
                                record.date = doc_info.get("date", "")
                                if not record.address:
                                    record.address = entry.address
                                append_record(record)
                                print(f"✓ gush={record.gush} addr={record.address[:30]}")
                            else:
                                print("⚠ not a permit")
                        except Exception as pdf_err:
                            print(f"⚠ {pdf_err}")

                    state.mark_file_processed(file_id, pdf_paths)
                    done += 1

                except Exception as e:
                    print(f"  ✗ Error: {e}")
                    state.mark_file_error(file_id, str(e))
                    done += 1

            if not _shutdown:
                state.phase = "done"
                state.save()

        # Summary
        progress = state.get_progress()
        print(f"\n{'='*50}")
        print(f"Done! {progress}")
        print(f"CSV: {CSV_PATH}")

    finally:
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

    # Find rows with missing key fields
    to_fix = [(i, r) for i, r in enumerate(rows) if not r.get("owner") or not r.get("architect")]
    print(f"Found {len(to_fix)} records with missing owner/architect out of {len(rows)} total.\n")

    fixed = 0
    for idx, row in to_fix:
        pdf = row.get("pdf_path", "")
        addr = row.get("address", "?")
        print(f"[{fixed+1}/{len(to_fix)}] File {row.get('building_file_number','?')}: {addr}")
        print(f"  Current: owner={row.get('owner','')!r}, architect={row.get('architect','')!r}")

        if pdf and Path(pdf).exists():
            # Open PDF in default viewer
            subprocess.Popen(["open", pdf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"  📄 Opened: {pdf}")
        else:
            print(f"  ⚠ PDF not found: {pdf}")

        print("  Fill in missing fields (press Enter to skip, 'q' to quit):")

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
        state.phase = "processing_files"
        state.save()

        # Process only the new ones
        if not CSV_PATH.exists():
            write_header()

        for file_id, entry in [(r["file_id"], state.files[r["file_id"]]) for r in new_entries]:
            if _shutdown:
                print(f"\n🛑 Stopped. Progress saved.")
                return

            try:
                print(f"\n  New file {file_id}: {entry.address}")
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
                    continue

                pdf_paths = []
                for doc_info in permits:
                    try:
                        dirname = sanitize_dirname(entry.address) or f"file_{file_id}"
                        nf = doc_info["url"].split("Name_File=")[1].split("&")[0]
                        filename = f"permit_{doc_info.get('entity_number') or nf}.pdf"
                        dest = PDF_DIR / dirname / filename
                        dest.parent.mkdir(parents=True, exist_ok=True)

                        await bc.download_pdf(doc_info["url"], str(dest))
                        pdf_paths.append(str(dest))

                        record = process_pdf(str(dest))
                        if record:
                            record.building_file_number = file_id
                            record.date = doc_info.get("date", "")
                            if not record.address:
                                record.address = entry.address
                            append_record(record)
                            print(f"  ✓ {record.address}")
                    except Exception as e:
                        print(f"  ⚠ {e}")

                state.mark_file_processed(file_id, pdf_paths)

            except Exception as e:
                print(f"  ✗ {e}")
                state.mark_file_error(file_id, str(e))

        print(f"\n✓ Update complete. Processed {len(new_entries)} new files.")

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
