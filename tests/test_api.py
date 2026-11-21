"""API client helpers: login crypto, secret scrubbing, form encoding."""

from __future__ import annotations

from sagemcom_rest import api


def test_sha512_crypt_glibc_vectors() -> None:
    # Reference vectors from the glibc SHA-crypt specification.
    assert api.sha512_crypt("Hello world!", "saltstring") == (
        "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJu"
        "esI68u4OTLiBFdcbYEdFCoEOfaS35inz1"
    )
    assert api.sha512_crypt("Hello world!", "saltstringsaltstring", rounds=10000) == (
        "$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sbHb"
        "bMCVNSnCM/UrjmM0Dp8vOuZeHBy/YTBmSK6H9qs/y3RnOaw5v."
    )


def test_parse_salt() -> None:
    assert api._parse_salt("abc") == ("abc", 5000)
    assert api._parse_salt("$6$rounds=8000$abc") == ("abc", 8000)


def test_scrub_removes_secrets_only() -> None:
    data = {
        "ssid": "Net",
        "passphrase": "hunter2",
        "wpa_key": "x",
        "radius_secret": "y",
        "nested": [{"Password": "z", "pingrespond": True, "mappings": []}],
    }
    assert api.scrub(data) == {
        "ssid": "Net",
        "nested": [{"pingrespond": True, "mappings": []}],
    }


def test_form_values() -> None:
    assert api._form_value(True) == "true"
    assert api._form_value(False) == "false"
    assert api._form_value(1) == "1"
    assert api._form_value("a b") == "a b"


def test_unwrap() -> None:
    assert api.unwrap([{"a": 1}]) == {"a": 1}
    assert api.unwrap([1, 2]) == [1, 2]
    assert api.unwrap(None) is None
