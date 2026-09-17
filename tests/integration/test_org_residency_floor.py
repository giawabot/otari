"""Organization residency floor (NorthRouter plan 01b), end to end.

The floor binds every request made with the organization's keys, with no
per-key escape: the anti-escape case (a member key with ``allowed_models:
null`` refused a non-sovereign model under a sovereign floor) is the whole
point of the feature, and the catalog honoring the floor without the query
param is what makes the policy discoverable.

The floor is set directly on the default organization's row (the endpoint
itself is covered by the endpoint tests below); attestations come from the
config ``sovereignty:`` blocks so no attestation rows are needed.
"""

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage
from sqlalchemy import select, update

from gateway.core.config import API_KEY_HEADER, API_ROOT, GatewayConfig
from gateway.models.tenancy import Organization
from gateway.models.usage import UsageLog

from .conftest import build_test_client

FLOOR_URL = f"{API_ROOT}/organizations/me/residency-floor"
_MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture
def floor_config(postgres_url: str) -> GatewayConfig:
    """Two instances: one unattested (level 1), one sovereign (level 3)."""
    return GatewayConfig(
        database_url=postgres_url,
        master_key="test-master-key",
        host="127.0.0.1",
        port=8000,
        auto_migrate=False,
        require_pricing=False,
        providers={
            "plainbox": {
                "provider_type": "openai",
                "api_base": "https://plain.example/v1",
                "api_key": "***",
                "models": ["m"],
            },
            "sovbox": {
                "provider_type": "openai",
                "api_base": "https://sov.example/v1",
                "api_key": "***",
                "models": ["m"],
                "sovereignty": {"level": 3, "hosted_in": "CA", "operator_jurisdiction": "CA"},
            },
        },
    )


@pytest.fixture
def floor_client(floor_config: GatewayConfig) -> Generator[TestClient]:
    yield from build_test_client(floor_config)


def _set_floor(db_session_factory: Any, floor: str | None) -> None:
    """Set the default organization's residency_floor directly."""
    session = db_session_factory()
    try:
        org_id = session.execute(select(Organization.id).limit(1)).scalars().first()
        assert org_id is not None
        session.execute(update(Organization).where(Organization.id == org_id).values(residency_floor=floor))
        session.commit()
    finally:
        session.close()


def _mint_key_header(client: TestClient, master_key_header: dict[str, str]) -> dict[str, str]:
    response = client.post(f"{API_ROOT}/keys", json={"key_name": "floor-test-key"}, headers=master_key_header)
    assert response.status_code == 200, response.text
    return {API_KEY_HEADER: f"Bearer {response.json()['key']}"}


def _completion(model: str) -> ChatCompletion:
    return ChatCompletion(
        id="chatcmpl-floor",
        object="chat.completion",
        created=0,
        model=model,
        choices=[
            {
                "index": 0,
                "message": ChatCompletionMessage(role="assistant", content="ok"),
                "finish_reason": "stop",
            }
        ],
        usage=CompletionUsage(prompt_tokens=5, completion_tokens=7, total_tokens=12),
    )


def _chat(client: TestClient, headers: dict[str, str], model: str, **extra: Any) -> Any:
    async def _acompletion(**_kwargs: Any) -> ChatCompletion:
        return _completion(model)

    with patch("gateway.api.routes.chat.acompletion") as mock:
        mock.side_effect = _acompletion
        return client.post(
            f"{API_ROOT}/chat/completions",
            json={"model": model, "messages": _MESSAGES, **extra},
            headers=headers,
        )


# =============================================================================
# The anti-escape case: a key with no per-key restriction is not exempt
# =============================================================================


def test_floor_refuses_a_non_sovereign_model_for_an_unrestricted_key(
    floor_client: TestClient,
    master_key_header: dict[str, str],
    db_session_factory: Any,
) -> None:
    api_key_header = _mint_key_header(floor_client, master_key_header)
    _set_floor(db_session_factory, "sovereign")
    response = _chat(floor_client, api_key_header, "plainbox:m")
    assert response.status_code == 403
    # The refusal names the organization as the source, not a caller-supplied bar.
    assert "This organization's residency policy" in response.text


def test_floor_still_serves_a_model_that_clears_it(
    floor_client: TestClient,
    master_key_header: dict[str, str],
    db_session_factory: Any,
) -> None:
    api_key_header = _mint_key_header(floor_client, master_key_header)
    _set_floor(db_session_factory, "sovereign")
    response = _chat(floor_client, api_key_header, "sovbox:m")
    assert response.status_code == 200, response.text
    # The audit row carries the floor as the bar's source, with no request bar.
    session = db_session_factory()
    try:
        row = (
            session.execute(select(UsageLog.residency_audit).where(UsageLog.residency_audit.is_not(None)))
            .scalars()
            .first()
        )
    finally:
        session.close()
    assert row is not None
    assert row["residency_source"] == "organization"
    assert row["requested_residency"] is None
    assert row["served_sovereignty_level"] == 3


def test_request_bar_still_raises_above_the_floor(
    floor_client: TestClient,
    master_key_header: dict[str, str],
    db_session_factory: Any,
) -> None:
    api_key_header = _mint_key_header(floor_client, master_key_header)
    # Floor 'canadian' (2) + request 'sovereign_model' (4): level 4 binds,
    # and the level-3 attestation no longer clears it.
    _set_floor(db_session_factory, "canadian")
    response = _chat(floor_client, api_key_header, "sovbox:m", residency="sovereign_model")
    assert response.status_code == 403
    assert "Residency policy 'sovereign_model'" in response.text


def test_no_floor_leaves_behavior_unchanged(
    floor_client: TestClient,
    master_key_header: dict[str, str],
) -> None:
    api_key_header = _mint_key_header(floor_client, master_key_header)
    response = _chat(floor_client, api_key_header, "plainbox:m")
    assert response.status_code == 200


# =============================================================================
# Catalog consistency
# =============================================================================


def test_catalog_honors_the_floor_without_the_query_param(
    floor_client: TestClient,
    master_key_header: dict[str, str],
    db_session_factory: Any,
) -> None:
    api_key_header = _mint_key_header(floor_client, master_key_header)
    _set_floor(db_session_factory, "sovereign")
    response = floor_client.get(f"{API_ROOT}/models", headers=api_key_header)
    assert response.status_code == 200
    ids = {m["id"] for m in response.json()["data"]}
    assert any("sovbox" in mid for mid in ids)
    assert not any("plainbox" in mid for mid in ids)


def test_catalog_without_floor_lists_both(
    floor_client: TestClient,
    master_key_header: dict[str, str],
) -> None:
    api_key_header = _mint_key_header(floor_client, master_key_header)
    response = floor_client.get(f"{API_ROOT}/models", headers=api_key_header)
    assert response.status_code == 200
    ids = {m["id"] for m in response.json()["data"]}
    assert any("sovbox" in mid for mid in ids)
    assert any("plainbox" in mid for mid in ids)


# =============================================================================
# The org endpoint
# =============================================================================


def test_endpoint_sets_and_clears_the_floor(
    client: TestClient,
    master_key_header: dict[str, str],
    db_session_factory: Any,
) -> None:
    response = client.put(FLOOR_URL, json={"residency_floor": "sovereign"}, headers=master_key_header)
    assert response.status_code == 200, response.text
    assert response.json()["residency_floor"] == "sovereign"

    session = db_session_factory()
    try:
        stored = session.execute(
            select(Organization.residency_floor).where(Organization.slug == response.json()["slug"])
        ).scalars().first()
    finally:
        session.close()
    assert stored == "sovereign"

    response = client.put(FLOOR_URL, json={"residency_floor": None}, headers=master_key_header)
    assert response.status_code == 200
    assert response.json()["residency_floor"] is None


def test_endpoint_refuses_an_unknown_bar(
    client: TestClient,
    master_key_header: dict[str, str],
) -> None:
    response = client.put(FLOOR_URL, json={"residency_floor": "european"}, headers=master_key_header)
    assert response.status_code == 400
    assert "unknown residency policy" in response.text
