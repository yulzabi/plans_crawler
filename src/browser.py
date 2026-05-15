"""Browser controller for Bartech portal interaction."""
import asyncio
import random
from playwright.async_api import async_playwright, Browser, Page, BrowserContext

BASE_URL = "https://nes.bartech-net.co.il"
SEARCH_URL = f"{BASE_URL}/SearchBuildingArchive"
DELAY_MIN = 8   # minimum seconds between page loads
DELAY_MAX = 15  # maximum seconds between page loads


async def _human_delay():
    await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


class BlockedError(Exception):
    """Raised when Cloudflare blocks us."""
    pass


class BrowserController:
    def __init__(self, subdomain: str = "nes"):
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self.base_url = f"https://{subdomain}.bartech-net.co.il"
        self.search_url = f"{self.base_url}/SearchBuildingArchive"

    async def launch(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=False)
        self._context = await self._browser.new_context(accept_downloads=True)
        self.page = await self._context.new_page()

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _check_blocked(self):
        """Check if Cloudflare blocked us. Raises BlockedError if so."""
        content = await self.page.content()
        if "Cloudflare" in content and ("blocked" in content or "Why have I been blocked" in content or "Ray ID" in content):
            raise BlockedError("🚫 Cloudflare blocked this IP. Stop and retry later with a different IP.")

    async def navigate_to_search(self):
        await self.page.goto(self.search_url, wait_until="networkidle")
        await self._check_blocked()
        await _human_delay()

    async def perform_search(self):
        """Fill search form for all Ness Ziona records and submit."""
        # Click the "search by land parcel" tab (איתור לפי מקרקעין)
        await self.page.locator("li.resp-tab-item:nth-child(2)").click()
        await asyncio.sleep(random.uniform(1, 3))

        # Leave גוש/חלקה/מגרש empty to get all results
        # Click the visible search button (the one for the active tab)
        await self.page.locator("button.g-recaptcha:visible").click()

        # Wait for CAPTCHA or results
        await self._handle_captcha_and_wait()

    async def _handle_captcha_and_wait(self):
        """Wait for results page — CAPTCHA may be invisible (auto) or manual."""
        print("\n⏳ Waiting for results page (CAPTCHA may auto-solve or require manual input)...")

        # Check if we already navigated to results (invisible CAPTCHA)
        if "SearchBuildingArchiveResults" in self.page.url:
            await self.page.wait_for_load_state("networkidle")
            # Check for error message on the page
            error = await self.page.locator("text=חלה שגיאה").count()
            if error:
                print("⚠ Portal returned an error. Retrying may help.")
                return False
            print("✓ Results page loaded (CAPTCHA auto-solved)")
            return True

        # Wait for navigation to results page (manual CAPTCHA case)
        print("🔐 If a CAPTCHA appears, please solve it in the browser window...")
        try:
            await self.page.wait_for_url("**/SearchBuildingArchiveResults/**", timeout=300_000)
        except Exception:
            if "SearchBuildingArchiveResults" not in self.page.url:
                raise RuntimeError("Timed out waiting for search results")

        await self.page.wait_for_load_state("networkidle")
        await _human_delay()
        print("✓ Results page loaded")
        return True

    async def get_results_list(self) -> list[dict]:
        """Parse the results table and return all building file entries."""
        return await self.page.evaluate("""() => {
            const rows = document.querySelectorAll('table tbody tr');
            const results = [];
            for (const row of rows) {
                const link = row.querySelector('a[href*="BuildingArchiveDetails"]');
                if (!link) continue;
                const cells = row.querySelectorAll('td');
                if (cells.length < 3) continue;
                const fileNum = cells[0].textContent.trim().split(/\\s/)[0];
                results.push({
                    file_id: fileNum,
                    address: cells[1].textContent.trim(),
                    parcel_info: cells[2].textContent.trim(),
                    url: link.getAttribute('href'),
                });
            }
            return results;
        }""")

    async def get_results_page_info(self) -> str:
        """Return the current page HTML for debugging/analysis."""
        return await self.page.content()

    async def navigate_to_file(self, url: str):
        """Navigate to a building file detail page."""
        full_url = url if url.startswith("http") else self.base_url + url
        await self.page.goto(full_url, wait_until="networkidle")
        await self._check_blocked()
        await _human_delay()

    async def get_pdf_links(self) -> list[dict]:
        """Parse the file detail page for document links. Returns list of {type, date, url, entity_number}."""
        return await self.page.evaluate("""() => {
            const rows = document.querySelectorAll('table tbody tr');
            const docs = [];
            for (const row of rows) {
                const cells = row.querySelectorAll('td');
                const link = row.querySelector('a[href*="DocumentViewer"]');
                if (!link || cells.length < 5) continue;
                docs.push({
                    doc_type: cells[0].textContent.trim(),
                    description: cells[1].textContent.trim(),
                    date: cells[2].textContent.trim(),
                    entity_type: cells[3].textContent.trim(),
                    entity_number: cells[4].textContent.trim(),
                    url: link.getAttribute('href'),
                });
            }
            return docs;
        }""")

    async def download_pdf(self, url: str, dest_path: str):
        """Download a PDF from a DocumentViewer URL."""
        from pathlib import Path
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        full_url = url if url.startswith("http") else self.base_url + url
        async with self.page.expect_download() as download_info:
            try:
                await self.page.goto(full_url)
            except Exception:
                pass  # 'Download is starting' error is expected
        download = await download_info.value
        await download.save_as(dest_path)
        await asyncio.sleep(random.uniform(1, 3))


async def test_browser():
    """Standalone test: full flow — search, collect, navigate file, list PDFs, download one."""
    bc = BrowserController()
    try:
        await bc.launch()
        print("✓ Browser launched")

        await bc.navigate_to_search()
        print("✓ Search page loaded")

        await bc.perform_search()
        print("✓ Search submitted, results loaded")

        results = await bc.get_results_list()
        print(f"✓ Found {len(results)} building files")
        for r in results[:3]:
            print(f"  {r['file_id']}: {r['address']} | {r['parcel_info']}")

        # Navigate to first file's detail page
        first = results[0]
        print(f"\nNavigating to file {first['file_id']}...")
        await bc.navigate_to_file(first["url"])
        docs = await bc.get_pdf_links()
        print(f"✓ Found {len(docs)} documents:")
        for d in docs:
            print(f"  [{d['doc_type']}] {d['date']} - {d['entity_number']} -> {d['url']}")

        # Download first היתר בניה PDF
        permits = [d for d in docs if "היתר בניה" in d["doc_type"]]
        if permits:
            p = permits[0]
            dest = "/tmp/test_download.pdf"
            print(f"\nDownloading permit PDF...")
            await bc.download_pdf(p["url"], dest)
            import os
            print(f"✓ Downloaded to {dest} ({os.path.getsize(dest)} bytes)")

    finally:
        await bc.close()


if __name__ == "__main__":
    asyncio.run(test_browser())
