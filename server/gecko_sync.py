#!/usr/bin/env python3
"""Gecko Log sync + reminder service (runs on the Pi behind nginx at /gecko-api/).

Same idea as study-sync: the app makes a random secret code, and anyone with the code can read and
write that one log and nothing else. Standard library plus `cryptography` and `requests`
(both already installed on the Pi), so nothing extra to install.

  GET    /gecko-api/vapid                 -> {"key": <public key for push subscriptions>}
  GET    /gecko-api/<code>/data           -> saved log JSON, or {}
  PUT    /gecko-api/<code>/data           -> save log JSON (max 4 MB); one dated copy kept per day, 30 days
  GET    /gecko-api/<code>/photo/<id>     -> JPEG
  PUT    /gecko-api/<code>/photo/<id>     -> save JPEG (max 3 MB)
  DELETE /gecko-api/<code>/photo/<id>
  PUT    /gecko-api/<code>/push           -> {"subscription": {...}, "time": "19:00", "tz": "Asia/Kuala_Lumpur"}
  DELETE /gecko-api/<code>/push
  POST   /gecko-api/<code>/push/test      -> sends a test notification now

Once a minute a loop checks each saved reminder; at the chosen local time it works out what is due
from the saved log (meal, calcium + D3, multivitamin, weigh-in) and sends one notification.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import struct
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(os.environ.get("GECKO_DATA", str(Path.home() / "gecko-sync" / "data")))
ROOT.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("GECKO_PORT", "8096"))
VAPID_SUB = "https://arifaqyl.me/gecko/"
MAX_DATA = 4 * 1024 * 1024
MAX_PHOTO = 3 * 1024 * 1024
MAX_CODES = 20
KEEP_DAYS = 30
CODE = r"([a-f0-9]{32,64})"
R_DATA = re.compile(rf"^/gecko-api/{CODE}/data$")
R_PHOTO = re.compile(rf"^/gecko-api/{CODE}/photo/([a-z0-9]{{6,40}})$")
R_PUSH = re.compile(rf"^/gecko-api/{CODE}/push$")
R_TEST = re.compile(rf"^/gecko-api/{CODE}/push/test$")
LOCK = threading.Lock()


# ---------- helpers ----------
def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def write_atomic(path: Path, raw: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
    os.replace(tmp, path)


def code_dir(code: str, create: bool) -> Path | None:
    d = ROOT / code
    if d.exists():
        return d
    if not create or len([p for p in ROOT.iterdir() if p.is_dir()]) >= MAX_CODES:
        return None
    d.mkdir()
    return d


# ---------- web push (VAPID + aes128gcm, RFC 8291 / 8292) ----------
def vapid_key() -> ec.EllipticCurvePrivateKey:
    path = ROOT.parent / "vapid_private.pem"
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), None)
    key = ec.generate_private_key(ec.SECP256R1())
    write_atomic(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(path, 0o600)
    return key


VAPID = vapid_key()
VAPID_PUB = VAPID.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def vapid_header(endpoint: str) -> str:
    u = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": VAPID_SUB}).encode())
    r, s = decode_dss_signature(VAPID.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    jwt = f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={b64u(VAPID_PUB)}"


def hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    ua_pub = unb64u(p256dh)
    auth_secret = unb64u(auth)
    eph = ec.generate_private_key(ec.SECP256R1())
    as_pub = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = os.urandom(16)
    cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", 4096, len(as_pub)) + as_pub + body


def send_push(sub: dict, message: dict) -> int:
    body = encrypt(json.dumps(message).encode(), sub["keys"]["p256dh"], sub["keys"]["auth"])
    r = requests.post(sub["endpoint"], data=body, timeout=15, headers={
        "Authorization": vapid_header(sub["endpoint"]),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": "43200",
        "Urgency": "normal",
    })
    return r.status_code


# ---------- what is due (mirrors the app's rules) ----------
STAGES = {
    "hatchling": {"feed": 1, "weigh": 7, "d3": 7, "multi": 7},
    "juvenile": {"feed": 1, "weigh": 7, "d3": 7, "multi": 7},
    "subadult": {"feed": 2, "weigh": 7, "d3": 10, "multi": 10},
    "adult": {"feed": 3, "weigh": 14, "d3": 14, "multi": 14},
}


def local_day(iso: str, tz: ZoneInfo) -> date:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz).date()


def stage_of(g: dict, today: date) -> str:
    if g.get("hatch"):
        try:
            months = (today - date.fromisoformat(g["hatch"])).days / 30.44
            return "hatchling" if months < 2 else "juvenile" if months < 7 else "subadult" if months < 12 else "adult"
        except ValueError:
            pass
    return g.get("stage") or "adult"


def due_today(log: dict, tz: ZoneInfo, today: date) -> list[str]:
    lines = []
    for g in log.get("geckos", []):
        st = STAGES[stage_of(g, today)]
        mine = sorted((l for l in log.get("logs", []) if l.get("g") == g.get("id")), key=lambda l: l["at"], reverse=True)
        feeds = [l for l in mine if l["t"] == "feed"]
        weights = [l for l in mine if l["t"] == "weight"]

        def since(entry):
            return (today - local_day(entry["at"], tz)).days

        bits = []
        every = g.get("feedEvery") or st["feed"]
        if not feeds or since(feeds[0]) >= every:
            dust = ""
            d3 = next((f for f in feeds if f["d"].get("dust") == "d3"), None)
            multi = next((f for f in feeds if f["d"].get("dust") == "multi"), None)
            if not d3 or since(d3) >= st["d3"]:
                dust = " Dust with calcium + D3."
            elif not multi or since(multi) >= st["multi"]:
                dust = " Dust with multivitamin."
            bits.append(f"{g['name']}’s meal is due.{dust}")
        if weights and since(weights[0]) >= st["weigh"]:
            bits.append(f"Time to weigh {g['name']}.")
        lines.extend(bits)
    return lines


def reminder_loop():
    while True:
        try:
            for d in ROOT.iterdir():
                push_file = d / "push.json"
                if not push_file.exists():
                    continue
                cfg = json.loads(push_file.read_text())
                try:
                    tz = ZoneInfo(cfg.get("tz") or "Asia/Kuala_Lumpur")
                except Exception:
                    tz = ZoneInfo("Asia/Kuala_Lumpur")
                now = datetime.now(tz)
                if now.strftime("%H:%M") != cfg.get("time", "19:00") or cfg.get("last") == now.date().isoformat():
                    continue
                data_file = d / "data.json"
                lines = due_today(json.loads(data_file.read_text()), tz, now.date()) if data_file.exists() else []
                cfg["last"] = now.date().isoformat()
                if lines:
                    status = send_push(cfg["subscription"], {"title": lines[0], "body": " ".join(lines[1:]) or "Tap to log it.", "url": "./?log=feed"})
                    cfg["lastStatus"] = status
                    if status in (404, 410):  # the phone unsubscribed
                        push_file.unlink()
                        continue
                write_atomic(push_file, json.dumps(cfg).encode())
        except Exception as exc:  # keep the loop alive, never crash the service
            print("reminder loop:", exc, flush=True)
        time.sleep(60 - datetime.now().second % 60 + 1)


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "gecko-sync"

    def _send(self, status, body=b"{}", ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self, limit):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if n <= 0 or n > limit:
            return None
        return self.rfile.read(n)

    def _path(self):
        return self.path.split("?")[0]

    def do_GET(self):
        p = self._path()
        if p == "/gecko-api/vapid":
            return self._send(200, json.dumps({"key": b64u(VAPID_PUB)}).encode())
        if m := R_DATA.match(p):
            f = ROOT / m.group(1) / "data.json"
            return self._send(200, f.read_bytes() if f.exists() else b"{}")
        if m := R_PHOTO.match(p):
            f = ROOT / m.group(1) / "photos" / f"{m.group(2)}.jpg"
            return self._send(200, f.read_bytes(), "image/jpeg") if f.exists() else self._send(404)
        if m := R_PUSH.match(p):
            f = ROOT / m.group(1) / "push.json"
            if not f.exists():
                return self._send(200, b'{"on":false}')
            cfg = json.loads(f.read_text())
            return self._send(200, json.dumps({"on": True, "time": cfg.get("time"), "tz": cfg.get("tz")}).encode())
        self._send(404)

    def do_PUT(self):
        p = self._path()
        if m := R_DATA.match(p):
            raw = self._body(MAX_DATA)
            if raw is None:
                return self._send(413)
            try:
                obj = json.loads(raw)
                assert isinstance(obj, dict) and isinstance(obj.get("logs"), list)
            except Exception:
                return self._send(400)
            d = code_dir(m.group(1), True)
            if not d:
                return self._send(429)
            with LOCK:
                write_atomic(d / "data.json", raw)
                snap = d / "daily" / f"{date.today().isoformat()}.json"
                write_atomic(snap, raw)
                for old in sorted((d / "daily").glob("*.json"))[:-KEEP_DAYS]:
                    old.unlink()
            return self._send(200, b'{"ok":true}')
        if m := R_PHOTO.match(p):
            raw = self._body(MAX_PHOTO)
            if raw is None or not raw.startswith(b"\xff\xd8"):
                return self._send(400)
            d = code_dir(m.group(1), True)
            if not d:
                return self._send(429)
            write_atomic(d / "photos" / f"{m.group(2)}.jpg", raw)
            return self._send(200, b'{"ok":true}')
        if m := R_PUSH.match(p):
            raw = self._body(16 * 1024)
            try:
                cfg = json.loads(raw)
                sub = cfg["subscription"]
                assert sub["endpoint"].startswith("https://") and sub["keys"]["p256dh"] and sub["keys"]["auth"]
                assert re.fullmatch(r"\d\d:\d\d", cfg.get("time", ""))
                ZoneInfo(cfg.get("tz") or "Asia/Kuala_Lumpur")
            except Exception:
                return self._send(400)
            d = code_dir(m.group(1), True)
            if not d:
                return self._send(429)
            write_atomic(d / "push.json", json.dumps({"subscription": sub, "time": cfg["time"], "tz": cfg.get("tz")}).encode())
            return self._send(200, b'{"ok":true}')
        self._send(404)

    def do_POST(self):
        if m := R_TEST.match(self._path()):
            f = ROOT / m.group(1) / "push.json"
            if not f.exists():
                return self._send(404)
            cfg = json.loads(f.read_text())
            try:
                status = send_push(cfg["subscription"], {"title": "Reminders are on", "body": f"You’ll hear from Gecko Log at {cfg['time']} when something is due.", "url": "./"})
            except Exception:
                return self._send(502)
            return self._send(200 if status < 300 else 502, json.dumps({"status": status}).encode())
        self._send(404)

    def do_DELETE(self):
        p = self._path()
        if m := R_PHOTO.match(p):
            f = ROOT / m.group(1) / "photos" / f"{m.group(2)}.jpg"
            f.unlink(missing_ok=True)
            return self._send(200, b'{"ok":true}')
        if m := R_PUSH.match(p):
            (ROOT / m.group(1) / "push.json").unlink(missing_ok=True)
            return self._send(200, b'{"ok":true}')
        self._send(404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=reminder_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
