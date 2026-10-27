"""Fetching issuers' signing keys: discovery, rotation, proxies and custom CA bundles."""

import datetime
import ipaddress
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from conftest import Signer
from marshal_ledger.oidc import IssuerConfig, TokenError, Verifier, _JwksResolver

PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


@pytest.fixture(autouse=True)
def no_proxy_settings(monkeypatch):
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


class Issuer:
    """A local OIDC issuer: serves discovery and JWKS, and records the requests it receives."""

    def __init__(self, tls=None):
        self.signers = {"key-1": Signer()}
        self.requests = []
        issuer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                # A proxy receives the absolute URL; an issuer receives only the path.
                issuer.requests.append(self.path)
                path = urlsplit(self.path).path
                if path.endswith("/.well-known/openid-configuration"):
                    base = self.path.removesuffix("/.well-known/openid-configuration")
                    base = base if base.startswith("http") else issuer.url + base
                    body = {"issuer": base, "jwks_uri": base + "/jwks"}
                elif path.endswith("/jwks"):
                    body = {"keys": [issuer.jwk(kid) for kid in issuer.signers]}
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        scheme = "http"
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
            scheme = "https"
        self.url = f"{scheme}://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def jwk(self, kid):
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.signers[kid].public_key))
        return {**key, "kid": kid, "use": "sig", "alg": "RS256"}

    def token(self, kid="key-1", iss=None):
        now = int(time.time())
        payload = {"iss": iss or self.url, "aud": "marshal", "iat": now, "exp": now + 300, "sub": "job"}
        return jwt.encode(payload, self.signers[kid].key, algorithm="RS256", headers={"kid": kid})

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def issuer():
    server = Issuer()
    yield server
    server.close()


def verifier(url, **config):
    return Verifier([IssuerConfig(issuer=url, provider="jenkins", repositories=["*"], **config)])


def test_keys_are_discovered_and_cached(issuer):
    check = verifier(issuer.url)
    assert check.verify(issuer.token()).repository == "job"
    check.verify(issuer.token())
    assert issuer.requests == ["/.well-known/openid-configuration", "/jwks"]


def test_configured_jwks_uri_skips_discovery(issuer):
    verifier(issuer.url, jwks_uri=issuer.url + "/jwks").verify(issuer.token())
    assert issuer.requests == ["/jwks"]


def test_rotated_key_is_fetched_but_unknown_keys_are_rate_limited(issuer, monkeypatch):
    check = verifier(issuer.url)
    check.verify(issuer.token())
    issuer.signers["key-2"] = Signer()
    # Within the miss interval, an unknown key id does not cause another fetch.
    with pytest.raises(TokenError, match="no signing key"):
        check.verify(issuer.token(kid="key-2"))
    assert issuer.requests.count("/jwks") == 1
    monkeypatch.setattr(_JwksResolver, "MISS_INTERVAL", 0)
    assert check.verify(issuer.token(kid="key-2")).repository == "job"
    assert issuer.requests.count("/jwks") == 2


def test_unreachable_issuer_is_a_token_error(issuer):
    url = "http://127.0.0.1:9"  # nothing listens here
    with pytest.raises(TokenError):
        verifier(url).verify(issuer.token(iss=url))


def test_http_proxy_is_honored(issuer, monkeypatch):
    # The issuer's host doesn't resolve; only the proxy can answer for it.
    url = "http://issuer.marshal.test/oidc"
    monkeypatch.setenv("HTTP_PROXY", issuer.url)
    check = verifier(url)
    assert check.verify(issuer.token(iss=url)).repository == "job"
    assert issuer.requests == [url + "/.well-known/openid-configuration", url + "/jwks"]


def test_no_proxy_bypasses_the_proxy(issuer, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # nothing listens here
    with pytest.raises(TokenError):
        verifier(issuer.url).verify(issuer.token())
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    assert verifier(issuer.url).verify(issuer.token()).repository == "job"


@pytest.fixture
def private_ca(tmp_path):
    """A self-signed certificate for 127.0.0.1, standing in for an internal CA."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "Marshal test issuer")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "issuer.pem", tmp_path / "issuer.key"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return str(cert_file), str(key_file)


def test_ssl_cert_file_trusts_an_internal_ca(private_ca, monkeypatch):
    issuer = Issuer(tls=private_ca)
    try:
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        with pytest.raises(TokenError, match="CERTIFICATE_VERIFY_FAILED|certificate verify failed"):
            verifier(issuer.url).verify(issuer.token())
        monkeypatch.setenv("SSL_CERT_FILE", private_ca[0])
        assert verifier(issuer.url).verify(issuer.token()).repository == "job"
    finally:
        issuer.close()
