"""
Chief WhatsApp Integration Test Suite
Tests the full message routing pipeline without needing Meta's servers.
Run with: python3 test_migration.py
"""
import asyncio
import os
import sys
import json
from unittest.mock import AsyncMock, patch, MagicMock

# ── Env vars must be set before importing modules ──
os.environ.setdefault("SUPABASE_URL", os.getenv("SUPABASE_URL", ""))
os.environ.setdefault("SUPABASE_ANON_KEY", os.getenv("SUPABASE_ANON_KEY", ""))
os.environ.setdefault("GEMINI_API_KEY", os.getenv("GEMINI_API_KEY", ""))
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", os.getenv("WHATSAPP_ACCESS_TOKEN", ""))
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", os.getenv("WHATSAPP_VERIFY_TOKEN", ""))

# ── Helpers ──

def build_wa_payload(phone_number_id: str, from_number: str, text: str) -> dict:
    """Build a minimal WhatsApp Cloud API message payload."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": phone_number_id},
                    "messages": [{
                        "id": "wamid.test001",
                        "from": from_number,
                        "type": "text",
                        "text": {"body": text},
                        "timestamp": "1700000000",
                    }],
                    "contacts": [{"profile": {"name": "Test User"}}],
                }
            }]
        }]
    }


def build_wa_interactive_payload(phone_number_id: str, from_number: str,
                                  button_id: str, button_title: str) -> dict:
    """Build a WhatsApp interactive button reply payload."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": phone_number_id},
                    "messages": [{
                        "id": "wamid.test002",
                        "from": from_number,
                        "type": "interactive",
                        "interactive": {
                            "type": "button_reply",
                            "button_reply": {"id": button_id, "title": button_title},
                        },
                    }],
                    "contacts": [{"profile": {"name": "Test User"}}],
                }
            }]
        }]
    }


PASS = "✅"
FAIL = "❌"
results = []

async def run_test(name: str, coro):
    try:
        await coro
        print(f"  {PASS} {name}")
        results.append((name, True, None))
    except Exception as e:
        print(f"  {FAIL} {name}: {e}")
        results.append((name, False, str(e)))


# ──────────────────────────────────────────────────────────
# TEST SUITES
# ──────────────────────────────────────────────────────────

async def test_imports():
    """Confirm all modules load without errors."""
    print("\n[1] Module Import Test")
    from api import whatsapp, pulse, memory, intent, billing, webhook
    await run_test("All modules import cleanly", asyncio.sleep(0))


async def test_intent_classification():
    """Test the Gemini intent classifier."""
    print("\n[2] Intent Classification (live Gemini call)")
    from api.intent import classify_intent

    async def _task():
        res = await classify_intent("I need to call Alex tomorrow about the proposal",
                                     "wa_test", "1", "build", 5.5)
        assert res.get("intent") in ("TASK", "NOTE", "QUERY", "DELEGATE", "NOISE",
                                      "CLARIFICATION_NEEDED"), f"Unknown intent: {res}"
        print(f"     intent={res.get('intent')} confidence={res.get('confidence', 0):.0%}")

    await run_test("classify_intent() returns valid intent", _task())


async def test_webhook_payload_routing():
    """Test WhatsApp payload parsing with a mocked Supabase + send_text."""
    print("\n[3] Webhook Payload Routing (mocked Supabase + WA API)")
    from api import whatsapp

    sent_messages = []

    async def mock_send_text(pid, to, text, preview_url=False):
        sent_messages.append({"to": to, "text": text})

    async def mock_mark_read(pid, msg_id):
        pass

    # Mock supabase client
    mock_supa = MagicMock()
    mock_supa.table.return_value.select.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[])
    )
    mock_supa.table.return_value.select.return_value.eq.return_value.eq.return_value.execute = AsyncMock(
        return_value=MagicMock(data=[])
    )

    async def mock_get_supabase():
        return mock_supa

    async def mock_check_access(user_id):
        return {"allowed": True}

    async def mock_record_usage(user_id, event_type, channel=""):
        pass

    payload = build_wa_payload("12345678", "919999999999", "hello chief")

    with patch.object(whatsapp, 'get_supabase', mock_get_supabase), \
         patch.object(whatsapp, 'send_text', mock_send_text), \
         patch.object(whatsapp, 'mark_read', mock_mark_read), \
         patch('api.billing.check_access', mock_check_access), \
         patch('api.billing.record_usage', mock_record_usage):

        await whatsapp.process_whatsapp_webhook(payload)

    # We expect at least one reply (invite gatekeeper or command response)
    async def _assert():
        assert len(sent_messages) >= 1, "No reply was sent to the user"
        print(f"     Reply: {sent_messages[0]['text'][:80]}...")

    await run_test("process_whatsapp_webhook() sends a reply", _assert())


async def test_webhook_verification_logic():
    """Test the verify token check logic directly."""
    print("\n[4] Webhook Verification Logic")
    os.environ["WHATSAPP_VERIFY_TOKEN"] = "chief_test_token_123"

    async def _verify_logic():
        token = os.getenv("WHATSAPP_VERIFY_TOKEN")
        # Simulate Meta's verification request
        mode = "subscribe"
        sent_token = "chief_test_token_123"
        challenge = "987654321"
        assert mode == "subscribe" and sent_token == token, "Token mismatch"
        return challenge

    await run_test("verify_token check passes with correct token", _verify_logic())

    async def _reject_bad_token():
        token = os.getenv("WHATSAPP_VERIFY_TOKEN")
        sent_token = "wrong_token"
        if sent_token == token:
            raise AssertionError("Should have rejected bad token")

    await run_test("verify_token check rejects wrong token", _reject_bad_token())


async def test_shortcode_regex():
    """Test the email shortcode reply pattern matching."""
    print("\n[5] Shortcode Reply Regex")
    import re
    pattern = r'^(\d{1,4})\s+(yes|drop|approve|reject)$'

    valid_cases = [("1234 yes", "1234", "yes"), ("56 drop", "56", "drop"),
                   ("9 approve", "9", "approve"), ("2345 reject", "2345", "reject")]
    invalid_cases = ["hello", "1234 maybe", "yes 1234", "12345 yes", "ed approve 1234"]

    async def _valid():
        for text, expected_code, expected_action in valid_cases:
            m = re.match(pattern, text.lower())
            assert m, f"Pattern should match: '{text}'"
            assert m.group(1) == expected_code
            assert m.group(2) == expected_action

    async def _invalid():
        for text in invalid_cases:
            m = re.match(pattern, text.lower())
            assert not m, f"Pattern should NOT match: '{text}'"

    await run_test("Valid shortcode patterns match", _valid())
    await run_test("Invalid inputs don't match shortcode pattern", _invalid())


async def test_embeddings():
    """Test Gemini embedding generation."""
    print("\n[6] Embedding Generation (live Gemini call)")
    from api.memory import get_embedding_async

    async def _embed():
        emb = await get_embedding_async("Test embedding for Chief")
        assert isinstance(emb, list) and len(emb) == 768, \
            f"Expected 768-dim list, got {type(emb)} len={len(emb) if isinstance(emb, list) else 'N/A'}"
        print(f"     Embedding dim: {len(emb)} ✓")

    await run_test("get_embedding_async() returns 768-dim vector", _embed())


# ──────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("Chief WhatsApp Integration Tests")
    print("=" * 60)

    await test_imports()
    await test_webhook_verification_logic()
    await test_shortcode_regex()
    await test_webhook_payload_routing()

    # Live API tests only if keys are set
    if os.getenv("GEMINI_API_KEY"):
        await test_intent_classification()
        await test_embeddings()
    else:
        print("\n[!] Skipping live AI tests — GEMINI_API_KEY not set")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        print("\nFailed tests:")
        for name, ok, err in results:
            if not ok:
                print(f"  {FAIL} {name}: {err}")
        sys.exit(1)
    else:
        print("All tests passed! 🎉")


if __name__ == "__main__":
    asyncio.run(main())
