#!/usr/bin/env python3
"""
회원가입 프로토타입 백엔드 (Python 표준 라이브러리 + SQLite, 설치 필요 없음)

실행:  python3 server.py            → http://localhost:8000
옵션:  python3 server.py --port 8080 --db signup.db

제공하는 것
  GET  /                      index.html
  GET  /api/health            서버 동작 확인
  GET  /api/users/check-id    아이디 중복확인      ?userId=...
  POST /api/email/check       이메일 가입 가능 여부 {email}
  POST /api/signup            회원가입 + 약관 동의 저장
  POST /api/consents          가입 직후 동의 내용 수정 (가입 응답의 consentToken 필요)

실제 본인인증·이메일 소유 확인은 하지 않는다. 시연·검증용 서버다.
"""
import argparse
import hashlib
import json
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
MAX_BODY = 16 * 1024  # 요청 본문 최대 16KB

# ---------- 입력 규칙 (index.html 의 프런트 규칙과 같게 유지) ----------
ID_RE = re.compile(r"^(?=.*[a-z])[a-z0-9]{6,20}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$")
PHONE_RE = re.compile(r"^01[0-9]-?\d{3,4}-?\d{4}$")
ZIP_RE = re.compile(r"^\d{5}$")
REQUIRED_CONSENTS = ("terms", "privacy", "age")
CHANNELS = ("이메일", "문자", "우편", "전화")


def password_ok(pw: str) -> bool:
    if not isinstance(pw, str) or not (8 <= len(pw) <= 20) or re.search(r"\s", pw):
        return False
    return all(re.search(p, pw) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9\s]"))


def hash_secret(value: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def token_digest(token: str) -> str:
    # 토큰은 충분히 긴 난수라서 빠른 해시로도 안전하게 찾을 수 있다
    return hashlib.sha256(token.encode()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- DB ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       TEXT NOT NULL UNIQUE,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  name          TEXT NOT NULL,
  phone         TEXT NOT NULL,
  zipcode       TEXT,
  address1      TEXT,
  address2      TEXT,
  created_at    TEXT NOT NULL
);
-- 아이디로 쓸 수 없는 예약어
CREATE TABLE IF NOT EXISTS reserved_ids (
  user_id TEXT PRIMARY KEY
);
-- 이전 가입 이력: withdrawn = 탈퇴(재가입 가능), blocked = 가입 제한
CREATE TABLE IF NOT EXISTS email_history (
  email      TEXT PRIMARY KEY,
  status     TEXT NOT NULL CHECK (status IN ('withdrawn', 'blocked')),
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
  user_pk        INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  terms          INTEGER NOT NULL,
  privacy        INTEGER NOT NULL,
  age            INTEGER NOT NULL,
  services       TEXT NOT NULL,   -- JSON: {"서비스명": true/false}
  marketing      TEXT NOT NULL,   -- JSON: {"point": {...}, "duty": {...}, "night": bool}
  token_hash     TEXT NOT NULL UNIQUE, -- 가입 직후 동의 수정용 토큰의 SHA-256
  agreed_at      TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
"""

# 시연용 초기 데이터
SEED_RESERVED = ["admin", "shinsegae", "test1234", "gudals4360"]
SEED_HISTORY = [
    ("rejoin@example.com", "withdrawn"),
    ("blocked@example.com", "blocked"),
]


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.Lock()
        with self.lock, self.conn:
            self.conn.executescript(SCHEMA)
            self.conn.executemany(
                "INSERT OR IGNORE INTO reserved_ids(user_id) VALUES (?)",
                [(i,) for i in SEED_RESERVED],
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO email_history(email, status, updated_at) VALUES (?, ?, ?)",
                [(e, s, now()) for e, s in SEED_HISTORY],
            )

    def id_available(self, user_id: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM users WHERE user_id = ? UNION SELECT 1 FROM reserved_ids WHERE user_id = ?",
                (user_id, user_id),
            ).fetchone()
        return row is None

    def email_status(self, email: str) -> str:
        """available | rejoin_available | unavailable
        이미 가입된 이메일과 가입 제한 이메일은 같은 값(unavailable)으로 돌려줘 사유를 드러내지 않는다."""
        email = email.lower()
        with self.lock:
            if self.conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                return "unavailable"
            row = self.conn.execute(
                "SELECT status FROM email_history WHERE email = ?", (email,)
            ).fetchone()
        if row is None:
            return "available"
        return "rejoin_available" if row["status"] == "withdrawn" else "unavailable"

    def create_user(self, data: dict, consents: dict) -> tuple[str, str]:
        token = secrets.token_urlsafe(24)
        email = data["email"].lower()
        addr = data.get("address") or {}
        with self.lock, self.conn:
            # 동시에 같은 아이디/이메일로 가입하는 경우를 막기 위해 트랜잭션 안에서 다시 확인
            if self.conn.execute(
                "SELECT 1 FROM users WHERE user_id = ? UNION SELECT 1 FROM reserved_ids WHERE user_id = ?",
                (data["userId"], data["userId"]),
            ).fetchone():
                raise Conflict("id_taken", "이미 사용 중인 아이디예요. 다른 아이디로 다시 확인해 주세요.")
            if self.conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() or \
               self.conn.execute(
                   "SELECT 1 FROM email_history WHERE email = ? AND status = 'blocked'", (email,)
               ).fetchone():
                raise Conflict("email_unavailable", "현재 이 이메일로 가입할 수 없어요. 다른 이메일을 사용해 주세요.")

            cur = self.conn.execute(
                """INSERT INTO users(user_id, email, password_hash, name, phone,
                                     zipcode, address1, address2, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["userId"], email, hash_secret(data["password"]),
                    data["name"].strip(), data["phone"].strip(),
                    addr.get("zipcode"), addr.get("address1"), addr.get("address2"),
                    now(),
                ),
            )
            user_pk = cur.lastrowid
            # 재가입이면 탈퇴 이력은 지운다 (현재 회원이 되었으므로)
            self.conn.execute("DELETE FROM email_history WHERE email = ?", (email,))
            self._write_consents(user_pk, consents, token_digest(token), insert=True)
        return data["userId"], token

    def update_consents(self, token: str, consents: dict) -> None:
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT user_pk FROM consents WHERE token_hash = ?", (token_digest(token),)
            ).fetchone()
            if row is None:
                raise HttpError(HTTPStatus.UNAUTHORIZED, "invalid_token", "동의 내용을 수정할 수 없어요. 다시 시도해 주세요.")
            self._write_consents(row["user_pk"], consents, None, insert=False)

    def _write_consents(self, user_pk: int, c: dict, token_hash: str | None, insert: bool) -> None:
        req = c["required"]
        values = (
            int(req["terms"]), int(req["privacy"]), int(req["age"]),
            json.dumps(c["services"], ensure_ascii=False),
            json.dumps(c["marketing"], ensure_ascii=False),
        )
        if insert:
            self.conn.execute(
                """INSERT INTO consents(user_pk, terms, privacy, age, services, marketing,
                                        token_hash, agreed_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_pk, *values, token_hash, now(), now()),
            )
        else:
            self.conn.execute(
                """UPDATE consents SET terms=?, privacy=?, age=?, services=?, marketing=?, updated_at=?
                   WHERE user_pk=?""",
                (*values, now(), user_pk),
            )


# ---------- 오류 ----------
class HttpError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Conflict(HttpError):
    def __init__(self, code: str, message: str):
        super().__init__(HTTPStatus.CONFLICT, code, message)


def bad_request(message: str) -> HttpError:
    return HttpError(HTTPStatus.BAD_REQUEST, "invalid_input", message)


# ---------- 입력 검증 ----------
def validate_signup(body: dict) -> dict:
    if not isinstance(body, dict):
        raise bad_request("요청 형식이 올바르지 않아요.")
    user_id = body.get("userId", "")
    if not isinstance(user_id, str) or not ID_RE.match(user_id):
        raise bad_request("아이디는 영문 소문자를 포함해 영문, 숫자 6~20자로 입력해 주세요.")
    if not password_ok(body.get("password", "")):
        raise bad_request("비밀번호 규칙을 확인해 주세요.")
    name = body.get("name", "")
    if not isinstance(name, str) or not (1 <= len(name.strip()) <= 30):
        raise bad_request("이름을 확인해 주세요.")
    phone = body.get("phone", "")
    if not isinstance(phone, str) or not PHONE_RE.match(phone.strip()):
        raise bad_request("휴대폰 번호를 확인해 주세요.")
    email = body.get("email", "")
    if not isinstance(email, str) or len(email) > 254 or not EMAIL_RE.match(email):
        raise bad_request("이메일 형식을 확인해 주세요.")
    addr = body.get("address")
    if addr is not None:
        if not isinstance(addr, dict) or not ZIP_RE.match(str(addr.get("zipcode", ""))) \
           or not isinstance(addr.get("address1"), str) or not addr["address1"].strip():
            raise bad_request("주소를 확인해 주세요.")
        if len(addr["address1"]) > 200 or len(str(addr.get("address2", ""))) > 200:
            raise bad_request("주소가 너무 길어요.")
    return body


def validate_consents(c) -> dict:
    if not isinstance(c, dict):
        raise bad_request("약관 동의 정보가 없어요.")
    req = c.get("required")
    if not isinstance(req, dict) or not all(req.get(k) is True for k in REQUIRED_CONSENTS):
        raise HttpError(HTTPStatus.BAD_REQUEST, "consent_required", "필수 약관에 모두 동의해 주세요.")
    services = c.get("services", {})
    marketing = c.get("marketing", {})
    if not isinstance(services, dict) or not all(isinstance(v, bool) for v in services.values()) \
       or len(services) > 20:
        raise bad_request("서비스 동의 정보가 올바르지 않아요.")
    if not isinstance(marketing, dict):
        raise bad_request("마케팅 동의 정보가 올바르지 않아요.")
    clean_mk = {
        g: {ch: bool((marketing.get(g) or {}).get(ch)) for ch in CHANNELS} for g in ("point", "duty")
    }
    clean_mk["night"] = bool(marketing.get("night"))
    return {"required": {k: True for k in REQUIRED_CONSENTS}, "services": services, "marketing": clean_mk}


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    store: Store = None  # main 에서 주입
    server_version = "SignupPrototype/1.0"

    def log_message(self, fmt, *args):  # 비밀번호 등이 로그에 남지 않도록 경로만 기록
        print(f"{self.command} {urlparse(self.path).path} -> {args[1] if len(args) > 1 else ''}")

    def _send(self, status, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _read_json(self):
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise HttpError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "invalid_content_type", "JSON으로 보내 주세요.")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large", "요청이 너무 커요.")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise bad_request("요청 형식이 올바르지 않아요.")

    def _handle(self, fn):
        try:
            fn()
        except HttpError as e:
            self._json(e.status, {"error": e.code, "message": e.message})
        except Exception as e:  # 예상하지 못한 오류는 내용을 숨긴다
            print("server error:", repr(e))
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR,
                       {"error": "server_error", "message": "잠시 후 다시 시도해 주세요."})

    def do_GET(self):
        self._handle(self._get)

    def do_POST(self):
        self._handle(self._post)

    def _get(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            # index.html 만 제공한다 (DB 파일 등 다른 파일은 노출하지 않음)
            self._send(HTTPStatus.OK, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True})
        elif url.path == "/api/users/check-id":
            user_id = (parse_qs(url.query).get("userId") or [""])[0]
            if not ID_RE.match(user_id):
                raise bad_request("아이디는 영문 소문자를 포함해 영문, 숫자 6~20자로 입력해 주세요.")
            self._json(HTTPStatus.OK, {"available": self.store.id_available(user_id)})
        else:
            raise HttpError(HTTPStatus.NOT_FOUND, "not_found", "찾을 수 없어요.")

    def _post(self):
        path = urlparse(self.path).path
        if path == "/api/email/check":
            email = self._read_json().get("email", "")
            if not isinstance(email, str) or not EMAIL_RE.match(email):
                raise bad_request("이메일 형식을 확인해 주세요.")
            self._json(HTTPStatus.OK, {"status": self.store.email_status(email)})
        elif path == "/api/signup":
            body = validate_signup(self._read_json())
            consents = validate_consents(body.get("consents"))
            user_id, token = self.store.create_user(body, consents)
            self._json(HTTPStatus.CREATED, {"ok": True, "userId": user_id, "consentToken": token})
        elif path == "/api/consents":
            body = self._read_json()
            token = body.get("consentToken")
            if not isinstance(token, str) or not token:
                raise HttpError(HTTPStatus.UNAUTHORIZED, "invalid_token", "동의 내용을 수정할 수 없어요.")
            self.store.update_consents(token, validate_consents(body.get("consents")))
            self._json(HTTPStatus.OK, {"ok": True})
        else:
            raise HttpError(HTTPStatus.NOT_FOUND, "not_found", "찾을 수 없어요.")


def main():
    parser = argparse.ArgumentParser(description="회원가입 프로토타입 백엔드")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", default=str(ROOT / "signup.db"))
    args = parser.parse_args()

    Handler.store = Store(Path(args.db))
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"http://{args.host}:{args.port} 에서 실행 중 (DB: {args.db}) — 끝내려면 Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
