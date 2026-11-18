import json

import httpx
import pytest

from app.durable_queue import (
    DurableKofiQueue,
    QueueConfigurationError,
    QueueUnavailable,
    QueueSettings,
)


def make_queue(handler):
    transport = httpx.MockTransport(handler)
    settings = QueueSettings(
        url="https://example.supabase.co",
        service_role_key="server-only-test-key",
    )
    return DurableKofiQueue(settings, transport=transport)


@pytest.mark.asyncio
async def test_claim_uses_private_service_role_rpc_and_returns_jobs():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[{"id": "job-1", "status": "processing"}])

    jobs = await make_queue(handler).claim("worker-test-01", limit=3)

    assert jobs == [{"id": "job-1", "status": "processing"}]
    assert seen["url"] == "https://example.supabase.co/rest/v1/rpc/claim_kofi_jobs"
    assert seen["headers"]["apikey"] == "server-only-test-key"
    assert seen["headers"]["authorization"] == "Bearer server-only-test-key"
    assert seen["body"] == {
        "p_worker_id": "worker-test-01",
        "p_limit": 3,
        "p_lease_seconds": 180,
    }


@pytest.mark.asyncio
async def test_complete_retry_and_quota_rpc_contracts():
    calls = []

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/complete_kofi_job"):
            return httpx.Response(200, json=True)
        if request.url.path.endswith("/retry_kofi_job"):
            return httpx.Response(200, json="retrying")
        return httpx.Response(200, json=False)

    queue = make_queue(handler)
    assert await queue.complete("job-1", "lease-1", {"text": "OK"}) is True
    assert await queue.retry("job-1", "lease-1", 30, "provider_busy") == "retrying"
    assert await queue.reserve_provider_quota("groq", "model-x", 5, 100, 6000, 900) is False

    assert [path.rsplit("/", 1)[-1] for path, _ in calls] == [
        "complete_kofi_job",
        "retry_kofi_job",
        "reserve_kofi_provider_quota",
    ]
    assert calls[1][1]["p_public_error_code"] == "provider_busy"


@pytest.mark.asyncio
async def test_upstream_failure_is_sanitized():
    queue = make_queue(
        lambda request: httpx.Response(503, text="internal secret database detail")
    )
    with pytest.raises(QueueUnavailable, match="HTTP 503") as exc:
        await queue.claim("worker-test")
    assert "internal secret" not in str(exc.value)


def test_invalid_configuration_and_claim_parameters_are_rejected():
    with pytest.raises(QueueConfigurationError):
        QueueSettings(url="not-a-url", service_role_key="secret")
    with pytest.raises(QueueConfigurationError):
        QueueSettings(url="https://example.supabase.co", service_role_key=" ")

    queue = DurableKofiQueue(
        QueueSettings(url="https://example.supabase.co", service_role_key="secret")
    )
    with pytest.raises(ValueError):
        import asyncio
        asyncio.run(queue.claim("x"))
