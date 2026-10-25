"""Settings: paths and credentials, read from the environment and optional .env files.

Lookup order for each variable: real environment > ``$JOBSWEEP_HOME/.env`` > ``./.env`` >
``~/.config/jobsweep/.env``. No third-party dotenv dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
CATALOG_DIR = PACKAGE_DIR / "catalog"
STATIC_DIR = PACKAGE_DIR / "web" / "static"
# Project dir = the folder that holds pyproject.toml when running from a checkout.
PROJECT_DIR = (
    PACKAGE_DIR.parent.parent if (PACKAGE_DIR.parent.parent / "pyproject.toml").exists() else Path.cwd()
)

# Every credential the sources understand. Keep in sync with .env.example and ARCHITECTURE.md.
ENV_VARS: dict[str, str] = {
    "USAJOBS_API_KEY": "USAJOBS Authorization-Key (free: https://developer.usajobs.gov/apirequest/)",
    "USAJOBS_EMAIL": "Email registered with USAJOBS, sent as User-Agent",
    "PIPEWORX_API_KEY": "Optional Pipeworx gateway key '<auth_key>:<email>' (USAJOBS fallback works without it, 50 calls/day)",
    "ADZUNA_APP_ID": "Adzuna app id (free: https://developer.adzuna.com/)",
    "ADZUNA_APP_KEY": "Adzuna app key",
    "THEIRSTACK_API_KEY": "TheirStack API key (credits: https://app.theirstack.com)",
    "HASDATA_API_KEY": "HasData API key for Indeed listings (free 1,000 credits/month)",
    "APIFY_TOKEN": "Apify API token (pay per result: hiring.cafe / job-search actors)",
    "SOCRATA_APP_TOKEN": "Optional NYC Open Data app token (raises rate limits)",
    "JOBSWEEP_DB": "Path of the SQLite database (default: <project>/var/jobsweep.db)",
    "JOBSWEEP_SITE_DIR": "Where `jobsweep export` writes the static board (default: <project>/site)",
    "JOBSWEEP_PROFILE": "Path to a profile.yaml overriding catalog/profile.yaml",
    "JOBSWEEP_HOME": "Folder holding a .env file with the variables above",
}


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _env_files() -> list[Path]:
    files = []
    home = os.environ.get("JOBSWEEP_HOME")
    if home:
        files.append(Path(home).expanduser() / ".env")
    files.append(Path.cwd() / ".env")
    files.append(PROJECT_DIR / ".env")
    files.append(Path.home() / ".config" / "jobsweep" / ".env")
    seen, out = set(), []
    for f in files:
        r = f.resolve() if f.exists() else f
        if r not in seen:
            seen.add(r)
            out.append(f)
    return out


def load_env() -> dict[str, str]:
    """Merged view of env files (lowest priority first) overlaid with the real environment."""
    merged: dict[str, str] = {}
    for f in reversed(_env_files()):
        merged.update(_parse_env_file(f))
    merged.update({k: v for k, v in os.environ.items() if k in ENV_VARS or k.startswith("JOBSWEEP_")})
    return {k: v for k, v in merged.items() if v != ""}


@dataclass
class Settings:
    env: dict[str, str] = field(default_factory=dict)
    db_path: Path = field(default_factory=lambda: PROJECT_DIR / "var" / "jobsweep.db")
    site_dir: Path = field(default_factory=lambda: PROJECT_DIR / "site")
    profile_path: Path | None = None
    user_agent: str = "jobsweep/0.1 (+https://github.com/gildedlily720/gradcafe_data)"

    @classmethod
    def load(cls) -> Settings:
        env = load_env()
        s = cls(env=env)
        if env.get("JOBSWEEP_DB"):
            s.db_path = Path(env["JOBSWEEP_DB"]).expanduser()
        if env.get("JOBSWEEP_SITE_DIR"):
            s.site_dir = Path(env["JOBSWEEP_SITE_DIR"]).expanduser()
        if env.get("JOBSWEEP_PROFILE"):
            s.profile_path = Path(env["JOBSWEEP_PROFILE"]).expanduser()
        return s

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.env.get(key, default)

    def has(self, *keys: str) -> bool:
        return all(self.env.get(k) for k in keys)
