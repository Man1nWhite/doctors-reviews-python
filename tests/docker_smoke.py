"""Offline smoke check for the built Linux image; needs no pytest or live sources."""

import asyncio
import importlib.util
import os
import sys
import tempfile

import httpx
from camoufox.pkgman import camoufox_path

sys.path.insert(0, "/app")

from collectors.browser import CamoufoxClient
from collectors.prodoctorov import parse_prodoctorov_page
from collectors.sberzdorovie import parse_sber_page


SBER_HTML = '''<title>Doctor</title><script id="__NEXT_DATA__">{"props":{"pageProps":{"preloadedState":{"doctorPage":{"doctor":{"reviewsForSeo":[{"text":"Хороший врач"}]}}}}}}</script>'''
PRO_HTML = '<title>Doctor</title><div class="b-review-card"><div class="b-review-card__comment">Хороший врач</div></div>'


async def check_browser(headless):
    with tempfile.TemporaryDirectory(prefix="camoufox-smoke-") as profile:
        browser = CamoufoxClient(
            profile_dir=profile,
            headless=headless,
            fingerprint_os=os.environ["BROWSER_OS"],
            proxy="",
        )
        try:
            context = await browser._get_context()
            page = await context.new_page()
            assert (await page.evaluate("navigator.platform")).startswith("Linux")
            await page.set_content(SBER_HTML)
            sber, _, _ = parse_sber_page(await page.content())
            assert sber.reviews[0].message == "Хороший врач"
            await page.set_content(PRO_HTML)
            pro = parse_prodoctorov_page(await page.content())
            assert pro.reviews[0].message == "Хороший врач"
        finally:
            await browser.close()
    print(f"Browser and both parsers OK: headless={headless}")


async def check_api():
    os.environ["STARTUP_WARMUP"] = "false"
    os.environ["API_AUTH_ENABLED"] = "false"
    os.environ["AI_API_KEY"] = "docker-smoke-test"
    import main

    async with main.lifespan(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
            assert response.status_code == 200 and response.json() == {"status": "ok"}
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/api/v1/getReviews")).status_code == 400
    print("API startup and routes OK")


async def main():
    assert os.environ.get("BROWSER_OS") == "linux"
    fonts = camoufox_path(download_if_missing=False) / "fonts"
    assert (fonts / "linux").is_dir()
    assert not (fonts / "windows").exists() and not (fonts / "macos").exists()
    assert importlib.util.find_spec("pytest") is None
    assert importlib.util.find_spec("fastapi_cli") is None
    async with asyncio.timeout(90):
        await check_browser(True)
        await check_browser("virtual")
        await check_api()
    print("Compact image smoke check passed")


if __name__ == "__main__":
    asyncio.run(main())
