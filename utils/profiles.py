"""Saved accounts (API credentials) and proxies, kept in Data/profiles.json.

Entries are only saved after a successful login, so a typo never ends up in the
list. The file lives in the private Data folder next to the login sessions.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from utils.config import Credentials, ProxySettings
from utils.filesystem import restrict_permissions

log = logging.getLogger(__name__)


@dataclass
class SavedAccount:
    api_id: int
    api_hash: str
    label: str            # the account's name as Telegram shows it
    last_used: float = 0.0

    def credentials(self) -> Credentials:
        return Credentials(self.api_id, self.api_hash)


@dataclass
class SavedProxy:
    protocol: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    rdns: bool = True
    last_used: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}|{self.username or ''}"

    @property
    def label(self) -> str:
        user = f" (user {self.username})" if self.username else ""
        return f"{self.protocol}://{self.host}:{self.port}{user}"

    def settings(self) -> ProxySettings:
        return ProxySettings(self.protocol, self.host, self.port, self.username, self.password, self.rdns)

    @staticmethod
    def from_settings(proxy: ProxySettings, last_used: float) -> SavedProxy:
        return SavedProxy(proxy.protocol, proxy.host, proxy.port, proxy.username, proxy.password, proxy.rdns, last_used)


class ProfileStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self.accounts: list[SavedAccount] = []
        self.proxies: list[SavedProxy] = []
        self.last_proxy: str | None = None  # key of the proxy used last time; None = no proxy
        self._load()

    # -- queries ------------------------------------------------------------------

    def accounts_by_recent(self) -> list[SavedAccount]:
        return sorted(self.accounts, key=lambda a: a.last_used, reverse=True)

    def proxies_by_recent(self) -> list[SavedProxy]:
        return sorted(self.proxies, key=lambda p: p.last_used, reverse=True)

    # -- changes ------------------------------------------------------------------

    def remember(self, credentials: Credentials, label: str, proxy: ProxySettings | None) -> None:
        """Save what just worked: the account, and the proxy if one was used."""
        now = time.time()
        account = next((a for a in self.accounts if a.api_id == credentials.api_id), None)
        if account is None:
            self.accounts.append(SavedAccount(credentials.api_id, credentials.api_hash, label, now))
        else:
            account.api_hash, account.label, account.last_used = credentials.api_hash, label, now

        if proxy is None:
            self.last_proxy = None
        else:
            fresh = SavedProxy.from_settings(proxy, now)
            self.proxies = [p for p in self.proxies if p.key != fresh.key] + [fresh]
            self.last_proxy = fresh.key
        self.save()

    def forget_account(self, api_id: int) -> None:
        self.accounts = [a for a in self.accounts if a.api_id != api_id]
        self.save()

    def forget_proxy(self, key: str) -> None:
        self.proxies = [p for p in self.proxies if p.key != key]
        if self.last_proxy == key:
            self.last_proxy = None
        self.save()

    # -- storage ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for entry in raw.get("accounts", []):
                self.accounts.append(SavedAccount(
                    int(entry["api_id"]), str(entry["api_hash"]), str(entry.get("label") or "Account"),
                    float(entry.get("last_used", 0)),
                ))
            for entry in raw.get("proxies", []):
                self.proxies.append(SavedProxy(
                    str(entry["protocol"]), str(entry["host"]), int(entry["port"]),
                    entry.get("username") or None, entry.get("password") or None,
                    bool(entry.get("rdns", True)), float(entry.get("last_used", 0)),
                ))
            last = raw.get("last_proxy")
            self.last_proxy = last if isinstance(last, str) else None
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("Could not read saved profiles (%s); starting with an empty list", exc)
            backup = self._path.with_name(self._path.name + ".bad")
            try:
                os.replace(self._path, backup)  # keep the unreadable file instead of overwriting it
            except OSError:
                pass
            self.accounts, self.proxies, self.last_proxy = [], [], None

    def save(self) -> None:
        data = {
            "version": 1,
            "last_proxy": self.last_proxy,
            "accounts": [
                {"api_id": a.api_id, "api_hash": a.api_hash, "label": a.label, "last_used": a.last_used}
                for a in self.accounts
            ],
            "proxies": [
                {"protocol": p.protocol, "host": p.host, "port": p.port, "username": p.username,
                 "password": p.password, "rdns": p.rdns, "last_used": p.last_used}
                for p in self.proxies
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_name(self._path.name + ".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        restrict_permissions(temp)  # holds the API hash and proxy password
        os.replace(temp, self._path)
