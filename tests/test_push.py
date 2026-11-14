"""Web Push: the payload must decrypt the way a browser decrypts it (RFC 8291), and the VAPID JWT must verify (RFC 8292)."""
import base64
import json
import os
import struct

import gecko_sync as gs
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def browser_keys():
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, pub, os.urandom(16)


def decrypt_like_a_browser(body: bytes, ua_key, ua_pub: bytes, auth: bytes) -> bytes:
    salt, rs, idlen = body[:16], struct.unpack("!I", body[16:20])[0], body[20]
    as_pub, ciphertext = body[21:21 + idlen], body[21 + idlen:]
    assert rs == 4096 and idlen == 65
    shared = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = gs.hkdf(auth, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    cek = gs.hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = gs.hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert plain.endswith(b"\x02"), "last record must end with the 0x02 delimiter"
    return plain[:-1]


def test_payload_round_trip():
    ua_key, ua_pub, auth = browser_keys()
    message = json.dumps({"title": "Nugget’s meal is due.", "body": "Dust with calcium + D3."}).encode()
    body = gs.encrypt(message, gs.b64u(ua_pub), gs.b64u(auth))
    assert decrypt_like_a_browser(body, ua_key, ua_pub, auth) == message


def test_every_payload_uses_a_fresh_salt_and_key():
    _, ua_pub, auth = browser_keys()
    a = gs.encrypt(b"same", gs.b64u(ua_pub), gs.b64u(auth))
    b = gs.encrypt(b"same", gs.b64u(ua_pub), gs.b64u(auth))
    assert a[:16] != b[:16] and a[21:86] != b[21:86]


def test_vapid_jwt_verifies_against_the_public_key():
    header = gs.vapid_header("https://fcm.googleapis.com/fcm/send/abc")
    assert header.startswith("vapid t=") and ", k=" in header
    token, key = header[len("vapid t="):].split(", k=")
    head_b64, claims_b64, sig_b64 = token.split(".")
    claims = json.loads(gs.unb64u(claims_b64))
    assert claims["aud"] == "https://fcm.googleapis.com"
    assert claims["sub"].startswith(("https://", "mailto:"))
    sig = gs.unb64u(sig_b64)
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), gs.unb64u(key))
    pub.verify(der, f"{head_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if invalid


def test_base64url_round_trip_without_padding():
    for n in range(0, 40):
        raw = os.urandom(n)
        enc = gs.b64u(raw)
        assert "=" not in enc and gs.unb64u(enc) == raw
