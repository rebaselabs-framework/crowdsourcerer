"""Tests for workers/local/pii.py — the local regex detector."""

import pytest

from workers.local.pii import ALL_ENTITIES, detect, redact, run


class TestEmailDetection:
    def test_basic_email(self):
        hits = detect("Contact me at alice@example.com for details")
        assert len(hits) == 1
        assert hits[0].type == "EMAIL"
        assert hits[0].value == "alice@example.com"

    def test_multiple_emails(self):
        hits = detect("cc: foo@a.co, bar@b.co, baz@c.co")
        emails = [h.value for h in hits if h.type == "EMAIL"]
        assert set(emails) == {"foo@a.co", "bar@b.co", "baz@c.co"}

    def test_plus_addressing(self):
        hits = detect("use alice+newsletter@example.com")
        assert any(h.type == "EMAIL" for h in hits)


class TestPhoneDetection:
    def test_us_format(self):
        hits = detect("Call me at +1 555-123-4567")
        assert any(h.type == "PHONE" for h in hits)

    def test_international(self):
        hits = detect("Our London office: +44 20 7946 0958")
        assert any(h.type == "PHONE" for h in hits)


class TestSSNDetection:
    def test_dashed_ssn(self):
        hits = detect("SSN: 123-45-6789")
        assert len(hits) == 1
        assert hits[0].type == "SSN"

    def test_invalid_ssn_prefixes_rejected(self):
        # 000-xx-xxxx and 666-xx-xxxx are invalid per SSA rules.
        assert detect("000-45-6789") == []
        assert detect("666-45-6789") == []

    def test_invalid_group_or_serial_rejected(self):
        assert detect("123-00-6789") == []
        assert detect("123-45-0000") == []


class TestCreditCardDetection:
    def test_visa_luhn_valid(self):
        # Valid Visa test number (Luhn-valid).
        hits = detect("Card: 4242 4242 4242 4242")
        types = [h.type for h in hits]
        assert "CREDIT_CARD" in types

    def test_random_digits_rejected(self):
        # 1234567812345678 is NOT Luhn-valid.
        hits = detect("Fake: 1234 5678 1234 5678")
        assert all(h.type != "CREDIT_CARD" for h in hits)

    def test_phone_and_card_not_double_counted(self):
        """A Luhn-valid card should not also be caught as a phone number."""
        hits = detect("Card: 4242 4242 4242 4242")
        assert sum(1 for h in hits if h.type == "CREDIT_CARD") == 1
        assert sum(1 for h in hits if h.type == "PHONE") == 0


class TestIPDetection:
    def test_ipv4(self):
        hits = detect("Origin: 192.168.1.1 / gateway 10.0.0.1")
        ips = [h.value for h in hits if h.type == "IPV4"]
        assert "192.168.1.1" in ips
        assert "10.0.0.1" in ips

    def test_ipv4_out_of_range_rejected(self):
        # 999.999.999.999 is not a valid IPv4.
        hits = detect("Bogus: 999.999.999.999")
        assert not [h for h in hits if h.type == "IPV4"]

    def test_ipv6(self):
        hits = detect("v6 host: 2001:0db8:85a3:0000:0000:8a2e:0370:7334 active")
        assert any(h.type == "IPV6" for h in hits)


class TestRedact:
    def test_redaction_replaces_hit_with_tag(self):
        text = "Email me at alice@example.com please"
        hits = detect(text)
        assert redact(text, hits) == "Email me at [EMAIL] please"

    def test_redaction_preserves_non_pii_text(self):
        text = "Hello world"
        assert redact(text, []) == "Hello world"

    def test_multiple_hits_redacted_in_order(self):
        text = "Call 555-123-4567 or email alice@example.com"
        hits = detect(text)
        out = redact(text, hits)
        assert "[EMAIL]" in out
        assert "[PHONE]" in out
        assert "alice@example.com" not in out


class TestRunEntry:
    def test_returns_api_shape(self):
        result = run({"text": "my email is alice@example.com"})
        assert "entities" in result
        assert "count" in result
        assert result["count"] == 1
        assert result["entities"][0]["type"] == "EMAIL"
        assert "start" in result["entities"][0]
        assert "end" in result["entities"][0]

    def test_mask_flag_returns_redacted_text(self):
        result = run({"text": "email alice@example.com", "mask": True})
        assert "redacted_text" in result
        assert "[EMAIL]" in result["redacted_text"]

    def test_mask_false_omits_redacted(self):
        result = run({"text": "email alice@example.com"})
        assert "redacted_text" not in result

    def test_missing_text_raises(self):
        with pytest.raises(ValueError):
            run({})

    def test_empty_text_raises(self):
        with pytest.raises(ValueError):
            run({"text": ""})

    def test_filter_by_entity_type(self):
        text = "alice@example.com and SSN 123-45-6789"
        result = run({"text": text, "entities": ["EMAIL"]})
        types = {e["type"] for e in result["entities"]}
        assert types == {"EMAIL"}


class TestAllEntitiesConstant:
    def test_contains_expected_types(self):
        expected = {
            "EMAIL",
            "PHONE",
            "SSN",
            "CREDIT_CARD",
            "IPV4",
            "IPV6",
            "IBAN",
            "PASSPORT",
        }
        assert ALL_ENTITIES == expected
