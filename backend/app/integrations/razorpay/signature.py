import hashlib
import hmac


def compute_webhook_signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = compute_webhook_signature(body, secret)
    return hmac.compare_digest(expected, signature)
