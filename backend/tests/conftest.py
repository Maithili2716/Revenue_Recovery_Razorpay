import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:test@localhost:5432/revenue_recovery_test",
)
os.environ.setdefault("RAZORPAY_KEY_ID", "test_key_id")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_key_secret")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret")

from app.api.razorpay_webhooks import get_idempotency_store
from app.integrations.razorpay.idempotency import InMemoryWebhookIdempotencyStore
from app.main import app

TEST_WEBHOOK_SECRET = os.environ["RAZORPAY_WEBHOOK_SECRET"]


def sign_webhook_body(body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def build_signed_request(
    payload: dict,
    *,
    secret: str = TEST_WEBHOOK_SECRET,
    extra_headers: dict[str, str] | None = None,
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": sign_webhook_body(body, secret),
    }
    if extra_headers:
        headers.update(extra_headers)
    return body, headers


@pytest.fixture
def webhook_store() -> InMemoryWebhookIdempotencyStore:
    return InMemoryWebhookIdempotencyStore()


@pytest.fixture
def client(webhook_store: InMemoryWebhookIdempotencyStore) -> TestClient:
    app.dependency_overrides[get_idempotency_store] = lambda: webhook_store
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
