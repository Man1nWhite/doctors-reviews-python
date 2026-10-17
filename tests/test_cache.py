import pytest

from app.cache import AsyncTTLCache


@pytest.mark.asyncio
async def test_cache_retains_stale_data_until_hard_expiry_without_extending_ttl():
    now = [0.0]
    cache = AsyncTTLCache(5, 2, clock=lambda: now[0], stale_ttl=10)
    await cache.set("doctor", {"reviews": ["Good"]})
    fresh = await cache.lookup("doctor")
    assert fresh.value == {"reviews": ["Good"]}
    assert not fresh.stale

    now[0] = 5
    assert await cache.get("doctor") is None
    stale = await cache.lookup("doctor")
    assert stale.value == fresh.value
    assert stale.stale
    now[0] = 14
    assert (await cache.lookup("doctor")).stale
    now[0] = 15
    assert await cache.lookup("doctor") is None


@pytest.mark.asyncio
async def test_successful_update_resets_fresh_and_stale_windows():
    now = [0.0]
    cache = AsyncTTLCache(5, 2, clock=lambda: now[0], stale_ttl=10)
    await cache.set("doctor", "Old")
    now[0] = 6
    await cache.set("doctor", "New")
    assert await cache.get("doctor") == "New"
    now[0] = 15
    stale = await cache.lookup("doctor")
    assert stale.value == "New"
    assert stale.stale
    now[0] = 21
    assert await cache.lookup("doctor") is None


@pytest.mark.asyncio
async def test_stale_entries_respect_capacity_and_can_be_deleted():
    now = [0.0]
    cache = AsyncTTLCache(5, 2, clock=lambda: now[0], stale_ttl=10)
    await cache.set("a", "A")
    await cache.set("b", "B")
    now[0] = 6
    assert (await cache.lookup("a")).stale
    await cache.set("c", "C")
    assert await cache.lookup("b") is None
    assert (await cache.lookup("a")).value == "A"
    await cache.delete("a")
    assert await cache.lookup("a") is None
    assert await cache.get("c") == "C"


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl, stale_ttl, override, expires_at", [(5, 0, None, 5), (0, 10, None, 0), (5, 10, 0, 0), (5, 10, 2, 12)])
async def test_disabled_cache_and_ttl_override(ttl, stale_ttl, override, expires_at):
    now = [0.0]
    cache = AsyncTTLCache(ttl, 2, clock=lambda: now[0], stale_ttl=stale_ttl)
    await cache.set("doctor", "Reviews", ttl=override)
    now[0] = expires_at
    assert await cache.lookup("doctor") is None
