"""
v4.1 NEW: Multi-user authorization.

3 role:
- owner   = plný přístup, může spravovat uživatele
- family  = ovládání vířivky + zobrazení dat
- guest   = jen čtení (žádné POST commands)

Každý uživatel má vlastní token. Tokeny se ukládají do users.json.
Zpětně kompatibilní: pokud existuje legacy `api.token` z v3.9, vytvoří se
implicit user "owner" s tím tokenem.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import HTTPException, Request

log = logging.getLogger("auth")


ROLE_OWNER = "owner"
ROLE_FAMILY = "family"
ROLE_GUEST = "guest"

VALID_ROLES = {ROLE_OWNER, ROLE_FAMILY, ROLE_GUEST}

# Permissions per role (write actions)
WRITE_PERMS = {
    ROLE_OWNER: True,
    ROLE_FAMILY: True,
    ROLE_GUEST: False,
}


@dataclass
class User:
    """Uživatel se jménem, rolí a SHA256 hash tokenu."""
    name: str
    role: str
    token_hash: str  # SHA256 hex
    created_at: float = field(default_factory=time.time)
    last_login: Optional[float] = None

    def to_public_dict(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "created_at": self.created_at,
            "last_login": self.last_login,
        }


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthConfig:
    """Multi-user auth singleton."""

    def __init__(
        self,
        enabled: bool = False,
        legacy_token: Optional[str] = None,
        users_file: Optional[Path] = None,
        auth_read_too: bool = False,
        allow_localhost: bool = True,
    ):
        self.enabled = enabled
        self.auth_read_too = auth_read_too
        self.allow_localhost = allow_localhost
        self.users_file = users_file
        self.users: Dict[str, User] = {}
        self._token_to_name: Dict[str, str] = {}

        if not enabled:
            log.info("API auth disabled")
            return

        # Load existing users
        self._load()

        # Backwards-compat: pokud nemáme uživatele a máme legacy token,
        # vytvoř implicit "owner"
        if not self.users and legacy_token and len(legacy_token) >= 8 \
                and not legacy_token.startswith("ZMENNAME"):
            log.info("Migrating legacy api.token -> implicit user 'owner'")
            self.add_user("owner", ROLE_OWNER, legacy_token)

        # Pokud auth_enabled ale žádní uživatelé, vygeneruj náhodný owner token
        if not self.users:
            random_token = secrets.token_urlsafe(32)
            log.warning("=" * 70)
            log.warning("Auth enabled but no users! Generated random owner token:")
            log.warning(f"  {random_token}")
            log.warning("Use this token to login as 'owner', then create more users via UI.")
            log.warning("Token IS persisted - won't change on next restart.")
            log.warning("=" * 70)
            try:
                self.add_user("owner", ROLE_OWNER, random_token)
            except Exception as e:
                log.error(f"Failed to add bootstrap owner: {e}")

        log.info(f"Auth enabled with {len(self.users)} users: {list(self.users.keys())}")

    def _load(self) -> None:
        if self.users_file is None or not self.users_file.exists():
            return
        try:
            with open(self.users_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data:
                u = User(**d)
                self.users[u.name] = u
                self._token_to_name[u.token_hash] = u.name
            log.info(f"Loaded {len(self.users)} users from {self.users_file}")
        except Exception as e:
            log.warning(f"Failed to load users.json: {e}")

    def _save(self) -> None:
        if self.users_file is None:
            return
        try:
            self.users_file.parent.mkdir(parents=True, exist_ok=True)
            data = [asdict(u) for u in self.users.values()]
            with open(self.users_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            try:
                self.users_file.chmod(0o600)
            except Exception:
                pass
        except Exception as e:
            log.error(f"Failed to save users.json: {e}")

    def add_user(self, name: str, role: str, token: str) -> User:
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid role: {role}")
        if not name or len(name) > 32 or not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Name must be alphanumeric (max 32 chars, _ and - allowed)")
        if name in self.users:
            raise ValueError(f"User '{name}' already exists")
        if not token or len(token) < 8:
            raise ValueError("Token must be at least 8 chars")
        token_hash = _hash_token(token)
        if token_hash in self._token_to_name:
            raise ValueError("Token already in use by another user")
        user = User(name=name, role=role, token_hash=token_hash)
        self.users[name] = user
        self._token_to_name[token_hash] = name
        self._save()
        log.info(f"Added user '{name}' (role={role})")
        return user

    def remove_user(self, name: str) -> None:
        if name not in self.users:
            raise ValueError(f"User '{name}' not found")
        owners = [u for u in self.users.values() if u.role == ROLE_OWNER]
        if self.users[name].role == ROLE_OWNER and len(owners) == 1:
            raise ValueError("Cannot remove last owner")
        u = self.users.pop(name)
        self._token_to_name.pop(u.token_hash, None)
        self._save()
        log.info(f"Removed user '{name}'")

    def update_role(self, name: str, new_role: str) -> None:
        if new_role not in VALID_ROLES:
            raise ValueError(f"Invalid role: {new_role}")
        if name not in self.users:
            raise ValueError(f"User '{name}' not found")
        if self.users[name].role == ROLE_OWNER and new_role != ROLE_OWNER:
            owners = [u for u in self.users.values() if u.role == ROLE_OWNER]
            if len(owners) == 1:
                raise ValueError("Cannot demote last owner")
        self.users[name].role = new_role
        self._save()
        log.info(f"User '{name}' role -> {new_role}")

    def regenerate_token(self, name: str) -> str:
        """Vygeneruje a uloží nový token, vrátí ho (jednou)."""
        if name not in self.users:
            raise ValueError(f"User '{name}' not found")
        new_token = secrets.token_urlsafe(32)
        old_hash = self.users[name].token_hash
        self._token_to_name.pop(old_hash, None)
        self.users[name].token_hash = _hash_token(new_token)
        self._token_to_name[self.users[name].token_hash] = name
        self._save()
        log.info(f"Token regenerated for user '{name}'")
        return new_token

    def authenticate(self, token: str) -> Optional[User]:
        if not token:
            return None
        token_hash = _hash_token(token)
        name = self._token_to_name.get(token_hash)
        if name is None:
            return None
        user = self.users.get(name)
        if user:
            user.last_login = time.time()
        return user

    def list_users(self) -> List[User]:
        return list(self.users.values())


_config: Optional[AuthConfig] = None


def init_auth(config: AuthConfig) -> None:
    global _config
    _config = config


def get_config() -> AuthConfig:
    return _config or AuthConfig(enabled=False)


def _extract_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    cookie_token = request.cookies.get("solarguard_token")
    if cookie_token:
        return cookie_token.strip()
    query_token = request.query_params.get("token")
    if query_token:
        return query_token.strip()
    return None


def _is_localhost(request: Request) -> bool:
    client = request.client
    if not client:
        return False
    return client.host in ("127.0.0.1", "::1", "localhost")


def get_user(request: Request) -> Optional[User]:
    cfg = get_config()
    if not cfg.enabled:
        return None
    token = _extract_token(request)
    if not token:
        return None
    return cfg.authenticate(token)


def require_auth(request: Request, write: bool = True, owner_only: bool = False) -> User:
    cfg = get_config()
    if not cfg.enabled:
        return User(name="anonymous", role=ROLE_OWNER, token_hash="")

    if not write and not cfg.auth_read_too:
        user = get_user(request)
        if user:
            return user
        return User(name="anonymous", role=ROLE_GUEST, token_hash="")

    if cfg.allow_localhost and _is_localhost(request):
        return User(name="localhost", role=ROLE_OWNER, token_hash="")

    user = get_user(request)
    if user is None:
        raise HTTPException(401, "Missing or invalid token")

    if write and not WRITE_PERMS.get(user.role, False):
        raise HTTPException(403, f"Role '{user.role}' cannot perform write actions")
    if owner_only and user.role != ROLE_OWNER:
        raise HTTPException(403, "Owner role required")

    return user


def check_token(token: str) -> Optional[User]:
    cfg = get_config()
    if not cfg.enabled:
        return User(name="anonymous", role=ROLE_OWNER, token_hash="")
    return cfg.authenticate(token)
