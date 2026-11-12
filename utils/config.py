"""Settings gathered at startup. Nothing is read from a config file."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_API_HASH_RE = re.compile(r"[0-9a-fA-F]{32}")

PROXY_PROTOCOLS = ("socks5", "socks4", "http")
DEFAULT_PROXY_PORT = {"socks5": 1080, "socks4": 1080, "http": 8080}


@dataclass(frozen=True)
class Credentials:
    api_id: int
    api_hash: str = field(repr=False)  # keep out of logs and tracebacks

    @staticmethod
    def parse(api_id: str, api_hash: str) -> Credentials:
        """Validate what the user typed. Raises ValueError with a message for the user."""
        api_id = api_id.strip()
        api_hash = api_hash.strip()
        if not api_id.isdigit() or int(api_id) <= 0:
            raise ValueError("The API ID is a number, for example 1234567.")
        if not _API_HASH_RE.fullmatch(api_hash):
            raise ValueError("The API hash is 32 letters and digits (0-9, a-f).")
        return Credentials(int(api_id), api_hash)


@dataclass(frozen=True)
class ProxySettings:
    protocol: str  # "socks5", "socks4" or "http"
    host: str
    port: int
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    rdns: bool = True  # let the proxy resolve DNS names too (recommended)

    @staticmethod
    def parse(protocol: str, host: str, port: str, username: str = "", password: str = "") -> ProxySettings:
        """Validate what the user typed. Raises ValueError with a message for the user."""
        if protocol not in PROXY_PROTOCOLS:
            raise ValueError("The protocol must be socks5, socks4 or http.")
        host = host.strip()
        if not host or " " in host:
            raise ValueError("Enter the proxy's host name or IP address.")
        if not port.strip().isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("The port is a number from 1 to 65535.")
        return ProxySettings(
            protocol=protocol,
            host=host,
            port=int(port),
            username=username.strip() or None,
            password=password or None,
        )

    def describe(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"

    def for_telethon(self) -> dict:
        """The proxy in the dict form Telethon accepts."""
        proxy: dict = {"proxy_type": self.protocol, "addr": self.host, "port": self.port, "rdns": self.rdns}
        if self.username:
            proxy["username"] = self.username
            proxy["password"] = self.password or ""
        return proxy


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @property
    def data_dir(self) -> Path:
        return self.root / "Data"  # database + login sessions

    @property
    def downloads_dir(self) -> Path:
        return self.root / "Downloads"

    @property
    def logs_dir(self) -> Path:
        return self.root / "Logs"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "archive.db"

    def session_path(self, api_id: int) -> Path:
        """Base path of the Telethon session for this API ID; Telethon appends '.session'."""
        return self.data_dir / f"session_{api_id}"
