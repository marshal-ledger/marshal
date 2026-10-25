from marshal_ledger.redact import REDACTED, redact


# Built by concatenation so the source never contains a credential-shaped literal.
FAKE_DATABRICKS_TOKEN = "dapi" + "0123456789abcdef" * 2
FAKE_AWS_KEY_ID = "AKIA" + "ABCDEFGHIJKLMNOP"


def test_redacts_known_credential_shapes():
    text = f"token {FAKE_DATABRICKS_TOKEN} and key {FAKE_AWS_KEY_ID}"
    cleaned, count = redact(text)
    assert count == 2
    assert "dapi" not in cleaned and "AKIA" not in cleaned
    assert cleaned.count(REDACTED) == 2


def test_leaves_ordinary_text_alone():
    text = "Use SUM(fare_amount) for revenue."
    assert redact(text) == (text, 0)
