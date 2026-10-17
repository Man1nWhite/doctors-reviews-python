import asyncio
from unittest.mock import AsyncMock, call

import httpx
import pytest
import pytest_asyncio

import main
from app.cache import AsyncTTLCache
from app.limiter import FixedWindowRateLimiter
from collectors.base import CollectorResult, Platform, Review, SourceBlockedError, SourceHTTPError


SBER_URL = "https://docdoc.ru/doctor/a"
PRO_URL = "https://prodoctorov.ru/doctor/a"


def review_result(message="Good", platform=Platform.SBERZDOROVIE):
    return CollectorResult(title="Doctor", reviews=[Review(id="1", message=message, rating=50, source=platform.value)])


@pytest.fixture(autouse=True)
def collector(monkeypatch):
    collect = AsyncMock(return_value=review_result())
    monkeypatch.setattr(main, "collect_with_browser", collect)
    return collect


@pytest_asyncio.fixture(autouse=True)
async def reset_main(monkeypatch):
    monkeypatch.setattr(main, "API_AUTH_ENABLED", False)
    monkeypatch.setattr(main, "API_KEY", "")
    monkeypatch.setattr(main, "STARTUP_WARMUP", False)
    monkeypatch.setattr(main, "MAX_RESULT_REVIEWS", 200)
    monkeypatch.setattr(main, "BLOCKED_CACHE_TTL", 30)
    monkeypatch.setattr(main, "metrics", dict.fromkeys(main.metrics, 0))
    main.app.state.cache = AsyncTTLCache(60, 10, stale_ttl=86400)
    main.app.state.blocked_cache = AsyncTTLCache(60, 10)
    main.app.state.collections = {}
    main.app.state.semaphore = asyncio.Semaphore(1)
    main.app.state.rate_limiter = FixedWindowRateLimiter(100, 60)
    yield
    tasks = list(main.app.state.collections.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def clock():
    now = [0.0]
    main.app.state.cache = AsyncTTLCache(5, 10, clock=lambda: now[0], stale_ttl=100)
    main.app.state.blocked_cache = AsyncTTLCache(30, 10, clock=lambda: now[0])
    return now


@pytest.mark.asyncio
async def test_failed_browser_returns_source_error(collector):
    collector.side_effect = SourceBlockedError("Browser verification did not complete")

    result = await main.fetch(SBER_URL, Platform.SBERZDOROVIE)

    assert result.status_code == 503
    collector.assert_awaited_once_with(SBER_URL, Platform.SBERZDOROVIE, False)


@pytest.mark.asyncio
async def test_source_timeout_returns_collection_error(collector):
    collector.side_effect = SourceHTTPError("timeout", status_code=504)
    result = await main.fetch(SBER_URL, Platform.SBERZDOROVIE)

    assert result.status_code == 504


@pytest.mark.asyncio
async def test_repeated_fetch_uses_cache(collector):
    first = await main.fetch(SBER_URL, Platform.SBERZDOROVIE)
    second = await main.fetch(SBER_URL, Platform.SBERZDOROVIE)

    assert first == second == review_result().public()
    collector.assert_awaited_once()


@pytest.mark.asyncio
async def test_both_platforms_use_browser_and_preserve_review_format(collector):
    collector.side_effect = [review_result(), review_result("Text", Platform.PRODOCTOROV)]

    sber = await main.fetch(SBER_URL, Platform.SBERZDOROVIE)
    prodoctorov = await main.fetch(PRO_URL, Platform.PRODOCTOROV)

    assert sber == review_result().public()
    assert prodoctorov == review_result("Text", Platform.PRODOCTOROV).public()
    assert collector.await_args_list == [call(SBER_URL, Platform.SBERZDOROVIE, False), call(PRO_URL, Platform.PRODOCTOROV, False)]


@pytest.mark.asyncio
async def test_blocked_result_uses_short_cache(collector, clock):
    collector.side_effect = SourceBlockedError("blocked")

    first = await main.fetch(SBER_URL, Platform.SBERZDOROVIE)
    second = await main.fetch(SBER_URL, Platform.SBERZDOROVIE)

    assert first.status_code == 503
    assert second.status_code == 503
    collector.assert_awaited_once()
    clock[0] = 31
    collector.side_effect = None
    assert await main.fetch(SBER_URL, Platform.SBERZDOROVIE) == review_result().public()
    assert collector.await_count == 2


@pytest.mark.asyncio
async def test_result_reviews_are_limited(monkeypatch, collector):
    monkeypatch.setattr(main, "MAX_RESULT_REVIEWS", 1)
    collector.return_value = CollectorResult(reviews=[Review(message="One"), Review(message="Two")])

    result = await main.fetch(SBER_URL, Platform.SBERZDOROVIE, all_reviews=True)

    assert len(result["reviews"]) == 1
    collector.assert_awaited_once_with(SBER_URL, Platform.SBERZDOROVIE, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform, url", [
    ("sberzdorovie", SBER_URL),
    ("prodoctorov", PRO_URL),
])
async def test_api_blocked_source_returns_controlled_error(collector, platform, url):
    collector.side_effect = SourceBlockedError("verification page")
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/getReviews", params={"url": url, "platform": platform, "all_reviews": "true"})

    assert response.status_code == 503
    assert response.json()["error"] == "source_blocked"


@pytest.mark.asyncio
async def test_api_returns_docdoc_reviews(collector):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/getReviews", params={"url": SBER_URL, "platform": "sberzdorovie", "all_reviews": "true"})

    assert response.status_code == 200
    assert response.json()["title"] == "Doctor"
    assert response.json()["reviews"][0]["message"] == "Good"
    assert response.headers["X-Reviews-Cache"] == "MISS"
    collector.assert_awaited_once_with(SBER_URL, Platform.SBERZDOROVIE, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform, url", [
    (Platform.SBERZDOROVIE, SBER_URL),
    (Platform.PRODOCTOROV, PRO_URL),
])
async def test_api_uses_and_caches_browser(collector, platform, url):
    collector.return_value = review_result("Хороший врач", platform)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/api/v1/getReviews", params={"url": url, "platform": platform.value})
        second = await client.get("/api/v1/getReviews", params={"url": url, "platform": platform.value})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["reviews"][0]["message"] == "Хороший врач"
    assert first.headers["X-Reviews-Cache"] == "MISS"
    assert second.headers["X-Reviews-Cache"] == "HIT"
    collector.assert_awaited_once_with(url, platform, False)


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_collection(collector):
    entered, release = asyncio.Event(), asyncio.Event()

    async def collect(*args):
        entered.set()
        await release.wait()
        return review_result()

    collector.side_effect = collect
    requests = [asyncio.create_task(main.fetch(SBER_URL, Platform.SBERZDOROVIE)) for _ in range(10)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert all(not request.done() for request in requests)
        assert main.metrics["coalesced_requests"] == 9
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*requests), 1)
        assert all(result == review_result().public() for result in results)
        collector.assert_awaited_once()
        assert not main.app.state.collections
    finally:
        release.set()
        await asyncio.gather(*requests, return_exceptions=True)


@pytest.mark.asyncio
async def test_page_and_all_reviews_do_not_share_collection_or_cache(collector):
    collector.side_effect = [review_result("Page"), review_result("All")]
    page, all_reviews = await asyncio.gather(
        main.fetch(PRO_URL, Platform.PRODOCTOROV),
        main.fetch(PRO_URL, Platform.PRODOCTOROV, True),
    )
    assert page["reviews"][0]["message"] == "Page"
    assert all_reviews["reviews"][0]["message"] == "All"
    assert await main.fetch(PRO_URL, Platform.PRODOCTOROV) == page
    assert await main.fetch(PRO_URL, Platform.PRODOCTOROV, True) == all_reviews
    assert collector.await_args_list == [call(PRO_URL, Platform.PRODOCTOROV, False), call(PRO_URL + "/otzivi", Platform.PRODOCTOROV, True)]


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_cancel_shared_collection(collector):
    entered, release = asyncio.Event(), asyncio.Event()

    async def collect(*args):
        entered.set()
        await release.wait()
        return review_result()

    collector.side_effect = collect
    first = asyncio.create_task(main.fetch(SBER_URL, Platform.SBERZDOROVIE))
    await asyncio.wait_for(entered.wait(), 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    collection = next(iter(main.app.state.collections.values()))
    assert not collection.done()
    second = asyncio.create_task(main.fetch(SBER_URL, Platform.SBERZDOROVIE))
    try:
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.wait_for(second, 1) == review_result().public()
        assert await main.fetch(SBER_URL, Platform.SBERZDOROVIE) == review_result().public()
        collector.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(second, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_api_response_returns_before_refresh_and_refreshes_only_once(collector, clock):
    old = await main.fetch(SBER_URL, Platform.SBERZDOROVIE)
    entered, release = asyncio.Event(), asyncio.Event()

    async def refresh(*args):
        entered.set()
        await release.wait()
        return review_result("Updated")

    collector.side_effect = refresh
    clock[0] = 6
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        params = {"url": SBER_URL, "platform": "sberzdorovie"}
        try:
            first = await asyncio.wait_for(client.get("/api/v1/getReviews", params=params), 1)
            await asyncio.wait_for(entered.wait(), 1)
            second = await asyncio.wait_for(client.get("/api/v1/getReviews", params=params), 1)
            assert first.status_code == second.status_code == 200
            assert first.json() == second.json() == old
            assert first.headers["X-Reviews-Cache"] == second.headers["X-Reviews-Cache"] == "STALE"
            assert collector.await_count == 2  # Initial collection plus one refresh.
            assert main.metrics["stale_cache_hits"] == 2
            release.set()
            await asyncio.wait_for(asyncio.gather(*main.app.state.collections.values()), 1)
            updated = await client.get("/api/v1/getReviews", params=params)
            assert updated.headers["X-Reviews-Cache"] == "HIT"
            assert updated.json() == review_result("Updated").public()
            assert collector.await_count == 2
        finally:
            release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [SourceBlockedError("blocked"), SourceHTTPError("timeout", status_code=504), RuntimeError("failed")])
async def test_failed_refresh_preserves_reviews_and_backs_off(collector, clock, error):
    old = await main.fetch(SBER_URL, Platform.SBERZDOROVIE)
    clock[0] = 6
    collector.side_effect = error
    assert await main.fetch(SBER_URL, Platform.SBERZDOROVIE) == old
    await asyncio.gather(*main.app.state.collections.values())
    assert await main.fetch(SBER_URL, Platform.SBERZDOROVIE) == old
    assert collector.await_count == 2
    assert main.metrics["errors"] == 1

    clock[0] = 37
    collector.side_effect = None
    collector.return_value = review_result("Recovered")
    assert await main.fetch(SBER_URL, Platform.SBERZDOROVIE) == old
    await asyncio.gather(*main.app.state.collections.values())
    assert await main.fetch(SBER_URL, Platform.SBERZDOROVIE) == review_result("Recovered").public()
    assert collector.await_count == 3


@pytest.mark.asyncio
async def test_stale_reviews_expire_even_when_refresh_fails(collector, clock):
    old = await main.fetch(SBER_URL, Platform.SBERZDOROVIE)
    clock[0] = 100
    collector.side_effect = SourceBlockedError("blocked")
    assert await main.fetch(SBER_URL, Platform.SBERZDOROVIE) == old
    await asyncio.gather(*main.app.state.collections.values())
    clock[0] = 105
    result = await main.fetch(SBER_URL, Platform.SBERZDOROVIE)
    assert result.status_code == 503
    assert collector.await_count == 2


@pytest.mark.asyncio
async def test_expired_cache_with_stale_disabled_waits_for_new_result(collector, clock):
    main.app.state.cache = AsyncTTLCache(5, 10, clock=lambda: clock[0])
    await main.fetch(SBER_URL, Platform.SBERZDOROVIE)
    clock[0] = 6
    collector.return_value = review_result("Updated")
    assert await main.fetch(SBER_URL, Platform.SBERZDOROVIE) == review_result("Updated").public()
    assert collector.await_count == 2


@pytest.mark.asyncio
async def test_unexpected_error_is_not_cached_as_empty_reviews(collector):
    collector.side_effect = RuntimeError("failed")
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        params = {"url": SBER_URL, "platform": "sberzdorovie"}
        response = await client.get("/api/v1/getReviews", params=params)
        assert response.status_code == 502
        assert response.json() == {"error": "collection_error"}
        collector.side_effect = None
        response = await client.get("/api/v1/getReviews", params=params)
    assert response.status_code == 200
    assert response.json() == review_result().public()
    assert collector.await_count == 2


@pytest.mark.asyncio
async def test_lifespan_closes_browser_on_error(monkeypatch):
    close = AsyncMock()
    monkeypatch.setattr(main, "close_browser", close)
    with pytest.raises(RuntimeError, match="shutdown"):
        async with main.lifespan(main.app):
            raise RuntimeError("shutdown")
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_cancels_background_refresh_before_closing_browser(monkeypatch, collector):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def refresh(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def close():
        assert cancelled.is_set()
        assert not main.app.state.collections

    close_browser = AsyncMock(side_effect=close)
    monkeypatch.setattr(main, "close_browser", close_browser)
    async with main.lifespan(main.app):
        now = [0.0]
        main.app.state.cache = AsyncTTLCache(5, 10, clock=lambda: now[0], stale_ttl=100)
        old = await main.fetch(SBER_URL, Platform.SBERZDOROVIE)
        now[0] = 6
        collector.side_effect = refresh
        assert await main.fetch(SBER_URL, Platform.SBERZDOROVIE) == old
        await asyncio.wait_for(entered.wait(), 1)
    close_browser.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_rate_limit_returns_retry_after():
    main.app.state.rate_limiter = FixedWindowRateLimiter(1, 60)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/metrics")).status_code == 200
        response = await client.get("/metrics")
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_api_auth_missing_configured_key():
    main.API_AUTH_ENABLED = True
    main.API_KEY = ""
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/metrics")
    assert response.status_code == 500
    assert response.json() == {"error": "server_configuration_error"}
