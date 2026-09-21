"""Invite-only identities and project permissions for one shared app server.

The catalogue stays on the server. Browsers never share a database file or a
provider credential, and project visibility is checked on every request.
"""

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from contextvars import ContextVar
from urllib.parse import urlsplit

from fastapi import HTTPException

LOCAL_ID = "local-owner"
ROLES = {"viewer": 0, "developer": 1, "maintainer": 2, "owner": 3}
SESSION_SECONDS = 12 * 60 * 60
INVITE_SECONDS = 48 * 60 * 60
current_user: ContextVar[dict | None] = ContextVar("sdlc_user", default=None)


def public_origin() -> str:
    value = os.environ.get("SDLC_PUBLIC_URL", "").rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (
            parsed.scheme != "https"
            and parsed.hostname not in {"localhost", "127.0.0.1"}
        )
    ):
        raise ValueError(
            "SDLC_PUBLIC_URL must be an HTTPS origin (HTTP is allowed on localhost for testing)"
        )
    return value


def email_address(value: str) -> str:
    value = value.strip().casefold()
    if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise ValueError("Enter a valid email address")
    return value


def identity(user: dict) -> dict:
    return {key: user[key] for key in ("id", "name")}


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: str | None = None) -> str:
    if not 12 <= len(password) <= 256:
        raise ValueError("Use a password between 12 and 256 characters")
    salt = salt or secrets.token_hex(16)
    derived = hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt),
        n=2**17,
        r=8,
        p=1,
        maxmem=256 * 1024 * 1024,
    )
    return f"scrypt${salt}${derived.hex()}"


def password_matches(password: str, stored: str) -> bool:
    # The same memory-hard calculation is used for unknown accounts.
    salt = stored.split("$")[1] if stored else "0" * 32
    try:
        actual = password_hash(password, salt)
    except ValueError:
        return False
    return bool(stored) and secrets.compare_digest(actual, stored)


class Collaboration:
    def __init__(self, store):
        self.store = store
        self.origin = public_origin()
        self.enabled = bool(self.origin)
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT UNIQUE, name TEXT NOT NULL,
                    password TEXT NOT NULL DEFAULT '', admin INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_members (
                    project_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL,
                    PRIMARY KEY(project_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS project_preferences (
                    project_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    hidden INTEGER NOT NULL DEFAULT 0, last_opened REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(project_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS invitations (
                    id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, project_id TEXT NOT NULL,
                    email TEXT NOT NULL, role TEXT NOT NULL, created_by TEXT NOT NULL,
                    expires REAL NOT NULL, used_at REAL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL,
                    status TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_attempts (
                    bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL
                );
            """)
            db.execute(
                "INSERT OR IGNORE INTO users(id,email,name,admin) VALUES (?,NULL,'Local owner',1)",
                (LOCAL_ID,),
            )
            migrated = db.execute(
                "SELECT value FROM metadata WHERE key='collaboration_schema'"
            ).fetchone()
            if not migrated:
                for row in db.execute("SELECT id,value FROM projects").fetchall():
                    project = json.loads(row["value"])
                    db.execute(
                        "INSERT OR IGNORE INTO project_members VALUES (?,?,?)",
                        (row["id"], LOCAL_ID, "owner"),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO project_preferences VALUES (?,?,?,?)",
                        (
                            row["id"],
                            LOCAL_ID,
                            int(bool(project.pop("removed", False))),
                            project.get("last_opened", 0),
                        ),
                    )
                    db.execute(
                        "UPDATE projects SET value=? WHERE id=?",
                        (json.dumps(project), row["id"]),
                    )
                db.execute("INSERT INTO metadata VALUES ('collaboration_schema','1')")

    def user(self, user_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT id,email,name,admin FROM users WHERE id=?", (user_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(401, "Sign in to continue")
        return dict(row)

    def actor(self) -> dict:
        return current_user.get() or self.user(LOCAL_ID)

    def setup_required(self) -> bool:
        with self.store.connect() as db:
            return not db.execute(
                "SELECT 1 FROM users WHERE password!='' LIMIT 1"
            ).fetchone()

    def session_user(self, token: str) -> dict | None:
        if not token or len(token) > 200:
            return None
        with self.store.connect() as db:
            row = db.execute(
                "SELECT u.id,u.email,u.name,u.admin FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token_hash=? AND s.expires>?",
                (token_hash(token), time.time()),
            ).fetchone()
        return dict(row) if row else None

    def session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?)",
                (token_hash(token), user_id, time.time() + SESSION_SECONDS),
            )
        return token

    def logout(self, token: str):
        with self.store.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))

    def throttle(self, ip: str, email: str):
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM auth_attempts WHERE expires<=?", (now,))
            for kind, value, maximum in [
                ("ip", ip, 50),
                ("account", email.strip().casefold(), 10),
            ]:
                bucket = token_hash(kind + ":" + value)
                row = db.execute(
                    "SELECT count FROM auth_attempts WHERE bucket=?", (bucket,)
                ).fetchone()
                if row and row["count"] >= maximum:
                    raise HTTPException(
                        429, "Too many sign-in attempts. Try again in 10 minutes."
                    )
                db.execute(
                    "INSERT INTO auth_attempts VALUES (?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1",
                    (bucket, now + 600),
                )

    def bootstrap(self, email: str, name: str, password: str) -> dict:
        email = email_address(email)
        hashed = password_hash(password)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM users WHERE password!='' LIMIT 1").fetchone():
                raise HTTPException(
                    409, "The workspace already has an owner. Sign in instead."
                )
            db.execute(
                "UPDATE users SET email=?,name=?,password=? WHERE id=?",
                (email, name.strip(), hashed, LOCAL_ID),
            )
        return self.user(LOCAL_ID)

    def login(self, email: str, password: str) -> dict:
        email = email_address(email)
        with self.store.connect() as db:
            row = db.execute(
                "SELECT id,password FROM users WHERE email=?", (email,)
            ).fetchone()
        if not password_matches(password, row["password"] if row else ""):
            raise HTTPException(401, "Email or password is incorrect")
        return self.user(row["id"])

    def role(self, project_id: str, user: dict | None = None) -> str | None:
        if not self.enabled:
            return "owner"
        user = user or self.actor()
        with self.store.connect() as db:
            row = db.execute(
                "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
                (project_id, user["id"]),
            ).fetchone()
        return row["role"] if row else None

    def require(self, project_id: str, minimum="viewer") -> str:
        role = self.role(project_id)
        if role is None:
            raise HTTPException(404, "Project not found")
        if ROLES[role] < ROLES[minimum]:
            raise HTTPException(403, f"This action requires a project {minimum}")
        return role

    def require_admin(self):
        if not self.actor()["admin"]:
            raise HTTPException(
                403, "Only the workspace administrator can change this setting"
            )

    def own_project(self, project_id: str):
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO project_members VALUES (?,?,?)",
                (project_id, self.actor()["id"], "owner"),
            )

    def preference(self, project_id: str, *, hidden: bool | None = None, opened=False):
        user = self.actor()
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO project_preferences(project_id,user_id) VALUES (?,?)",
                (project_id, user["id"]),
            )
            if hidden is not None:
                db.execute(
                    "UPDATE project_preferences SET hidden=? WHERE project_id=? AND user_id=?",
                    (int(hidden), project_id, user["id"]),
                )
            if opened:
                db.execute(
                    "UPDATE project_preferences SET last_opened=? WHERE project_id=? AND user_id=?",
                    (time.time(), project_id, user["id"]),
                )

    def projects(self, *, hidden=False) -> list[dict]:
        user = self.actor()
        with self.store.connect() as db:
            prefs = {
                row["project_id"]: dict(row)
                for row in db.execute(
                    "SELECT * FROM project_preferences WHERE user_id=?", (user["id"],)
                )
            }
        result = []
        for project in self.store.all("projects"):
            role = self.role(project["id"])
            pref = prefs.get(project["id"], {})
            if (
                role
                and bool(pref.get("hidden", project.get("removed", False))) == hidden
            ):
                result.append(
                    {**project, "role": role, "last_opened": pref.get("last_opened", 0)}
                )
        return result

    def members(self, project_id: str) -> list[dict]:
        with self.store.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT u.id,u.name,u.email,m.role FROM project_members m JOIN users u ON u.id=m.user_id WHERE m.project_id=? ORDER BY u.name",
                    (project_id,),
                )
            ]

    def invite(self, project_id: str, email: str, role: str) -> dict:
        self.require(project_id, "owner")
        email = email_address(email)
        if role not in ROLES or role == "owner":
            raise ValueError("Choose viewer, developer, or maintainer")
        token, invitation_id = secrets.token_urlsafe(32), uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute(
                "DELETE FROM invitations WHERE project_id=? AND email=? AND used_at IS NULL",
                (project_id, email),
            )
            db.execute(
                "INSERT INTO invitations VALUES (?,?,?,?,?,?,?,NULL)",
                (
                    invitation_id,
                    token_hash(token),
                    project_id,
                    email,
                    role,
                    self.actor()["id"],
                    time.time() + INVITE_SECONDS,
                ),
            )
        return {
            "id": invitation_id,
            "email": email,
            "role": role,
            "url": self.origin + "/#invite=" + token,
            "expires": time.time() + INVITE_SECONDS,
        }

    def accept_invite(self, token: str, email: str, name: str, password: str) -> dict:
        email = email_address(email)
        with self.store.connect() as db:
            account = db.execute(
                "SELECT id,password FROM users WHERE email=?", (email,)
            ).fetchone()
        if account and not password_matches(password, account["password"]):
            raise HTTPException(
                401, "Use your existing account password to accept this invitation"
            )
        hashed = account["password"] if account else password_hash(password)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            invite = db.execute(
                "SELECT * FROM invitations WHERE token_hash=? AND email=? AND used_at IS NULL AND expires>?",
                (token_hash(token), email, time.time()),
            ).fetchone()
            if not invite:
                raise HTTPException(400, "This invitation is invalid or expired")
            # Membership revocation also invalidates invitations issued by that owner.
            owner = db.execute(
                "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
                (invite["project_id"], invite["created_by"]),
            ).fetchone()
            if not owner or owner["role"] != "owner":
                raise HTTPException(400, "This invitation is no longer valid")
            existing = db.execute(
                "SELECT id,password FROM users WHERE email=?", (email,)
            ).fetchone()
            if bool(existing) != bool(account) or (
                existing and existing["password"] != hashed
            ):
                raise HTTPException(
                    409, "Your account changed while joining. Please try again."
                )
            user_id = existing["id"] if existing else uuid.uuid4().hex
            if not existing:
                db.execute(
                    "INSERT INTO users VALUES (?,?,?,?,0)",
                    (user_id, email, name.strip(), hashed),
                )
            # An invitation never demotes an existing member or owner.
            member = db.execute(
                "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
                (invite["project_id"], user_id),
            ).fetchone()
            if not member:
                db.execute(
                    "INSERT INTO project_members VALUES (?,?,?)",
                    (invite["project_id"], user_id, invite["role"]),
                )
            db.execute(
                "UPDATE invitations SET used_at=? WHERE id=?",
                (time.time(), invite["id"]),
            )
        return self.user(user_id)

    def change_member(self, project_id: str, user_id: str, role: str | None):
        self.require(project_id, "owner")
        if role is not None and role not in ROLES:
            raise ValueError("Unknown project role")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            member = db.execute(
                "SELECT role FROM project_members WHERE project_id=? AND user_id=?",
                (project_id, user_id),
            ).fetchone()
            if not member:
                raise HTTPException(404, "Project member not found")
            if member["role"] == "owner" and role != "owner":
                count = db.execute(
                    "SELECT count(*) AS n FROM project_members WHERE project_id=? AND role='owner'",
                    (project_id,),
                ).fetchone()["n"]
                if count <= 1:
                    raise HTTPException(409, "Keep at least one project owner")
            if role is None:
                db.execute(
                    "DELETE FROM project_members WHERE project_id=? AND user_id=?",
                    (project_id, user_id),
                )
            else:
                db.execute(
                    "UPDATE project_members SET role=? WHERE project_id=? AND user_id=?",
                    (role, project_id, user_id),
                )
            db.execute(
                "DELETE FROM invitations WHERE project_id=? AND created_by=? AND used_at IS NULL",
                (project_id, user_id),
            )

    def review_view(self, review: dict) -> dict:
        user_id = self.actor()["id"]
        return {
            **review,
            "checkpoints": review.get("reviewers", {}).get(
                user_id, review["checkpoints"] if user_id == LOCAL_ID else {}
            ),
        }

    def save_checkpoints(self, review: dict, checkpoints: dict):
        user = self.actor()
        for value in checkpoints.values():
            value["reviewer"] = identity(user)
        review.setdefault("reviewers", {})[user["id"]] = checkpoints
        if user["id"] == LOCAL_ID:
            review["checkpoints"] = checkpoints

    def audit(
        self,
        project_id: str,
        action: str,
        *,
        task_id="",
        detail=None,
        status="completed",
    ) -> str:
        event_id = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    project_id,
                    task_id,
                    json.dumps(identity(self.actor())),
                    action,
                    json.dumps(detail or {}),
                    status,
                    time.time(),
                ),
            )
        return event_id

    def finish_audit(self, event_id: str, status="completed"):
        with self.store.connect() as db:
            db.execute(
                "UPDATE audit_events SET status=? WHERE id=?", (status, event_id)
            )

    def events(self, project_id: str, task_id="") -> list[dict]:
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM audit_events WHERE project_id=? AND (?='' OR task_id=?) ORDER BY created_at DESC LIMIT 100",
                (project_id, task_id, task_id),
            ).fetchall()
        return [
            {
                **dict(row),
                "actor": json.loads(row["actor"]),
                "detail": json.loads(row["detail"]),
            }
            for row in rows
        ]
