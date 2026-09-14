from cua.safety.redaction import MASK, Redactor


def test_pattern_redaction():
    r = Redactor()
    assert "123-45-6789" not in r.redact("ssn 123-45-6789")
    assert r.redact("card 4111 1111 1111 1111").count(MASK) == 1
    assert "sk-abc12345" not in r.redact("key sk-abc12345 done")
    assert "a@b.com" not in r.redact("email a@b.com")


def test_secret_value_redaction():
    r = Redactor(["demo"])
    assert r.redact("passcode is demo") == "passcode is " + MASK


def test_add_secret_and_nested_objects():
    r = Redactor()
    r.add_secret("s3cr3t")
    obj = {"a": ["x s3cr3t", {"b": "ssn 111-22-3333"}], "n": 5}
    out = r.redact_obj(obj)
    assert "s3cr3t" not in str(out)
    assert "111-22-3333" not in str(out)
    assert out["n"] == 5  # non-strings untouched


def test_non_string_passthrough():
    r = Redactor()
    assert r.redact(None) is None
    assert r.redact("") == ""
