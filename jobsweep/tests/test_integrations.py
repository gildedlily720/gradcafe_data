"""Connector/integration files: .mcp.json, Claude settings, Desktop config, setup script, skill, docs.

No network: the setup script runs in --dry-run mode or against a fake ``claude`` executable.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from jobsweep.config import ENV_VARS

JOBSWEEP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = JOBSWEEP_DIR.parent
INTEGRATIONS = JOBSWEEP_DIR / "integrations"

MCP_JSON = REPO_ROOT / ".mcp.json"
CLAUDE_SETTINGS = REPO_ROOT / ".claude" / "settings.json"
SKILL_MD = REPO_ROOT / ".claude" / "skills" / "jobsweep" / "SKILL.md"
DESKTOP_JSON = INTEGRATIONS / "claude_desktop_config.json"
SETUP_SH = INTEGRATIONS / "claude_code_setup.sh"
ENV_EXAMPLE = JOBSWEEP_DIR / ".env.example"
CONNECTORS_MD = JOBSWEEP_DIR / "docs" / "CONNECTORS.md"
LANES_YAML = JOBSWEEP_DIR / "src" / "jobsweep" / "catalog" / "lanes.yaml"

# Servers that need no secret and no sign-in: the only ones auto-enabled for the project.
NO_SECRET_SERVERS = {"jobsweep", "pipeworx-usajobs", "exa", "jobspy", "ats-jobs", "playwright"}
# OAuth/metered or key-requiring servers: present in .mcp.json but left for the user to approve.
NEEDS_AUTH_SERVERS = {"apify", "theirstack", "hasdata-indeed", "adzuna", "federal-compass"}
# Read-only jobsweep MCP tools (ARCHITECTURE.md section 10) that may be pre-allowed.
READ_ONLY_JOBSWEEP_TOOLS = {
    "list_lanes",
    "list_sources",
    "list_employers",
    "search_jobs",
    "get_job",
    "digest",
    "sweep_status",
}
ALL_JOBSWEEP_TOOLS = READ_ONLY_JOBSWEEP_TOOLS | {
    "set_job_status",
    "run_sweep",
    "live_search",
    "check_employer",
    "import_jobs",
}
# Credentials (not paths) from config.ENV_VARS.
CREDENTIAL_VARS = {k for k in ENV_VARS if not k.startswith("JOBSWEEP_")}

PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")
SAFE_PLACEHOLDER_RE = re.compile(r"^([A-Z_][A-Z0-9_]*):-([^}]*)$")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def strings_in(obj) -> list[str]:  # noqa: ANN001
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for k, v in obj.items() for s in strings_in(k) + strings_in(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings_in(v)]
    return []


def expand_like_claude_code(value: str, env: dict[str, str]) -> str:
    """``${VAR}`` / ``${VAR:-default}`` expansion; raises KeyError for an unset ``${VAR}`` without default."""

    def repl(m: re.Match) -> str:
        inner = m.group(1)
        name, sep, default = inner.partition(":-")
        if name in env and env[name] != "":
            return env[name]
        if sep:
            return default
        raise KeyError(name)

    return PLACEHOLDER_RE.sub(repl, value)


# ---------------------------------------------------------------------------------------------------------
# .mcp.json


def test_mcp_json_is_valid_and_well_formed() -> None:
    data = load_json(MCP_JSON)
    assert set(data) == {"mcpServers"}
    servers = data["mcpServers"]
    assert NO_SECRET_SERVERS | NEEDS_AUTH_SERVERS == set(servers)
    for name, entry in servers.items():
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", name), name
        kind = entry.get("type", "stdio")
        if kind == "stdio":
            assert set(entry) <= {"type", "command", "args", "env"}, name
            assert isinstance(entry["command"], str) and entry["command"], name
            assert isinstance(entry.get("args", []), list), name
            assert all(isinstance(a, str) and a for a in entry.get("args", [])), name
            env = entry.get("env", {})
            assert isinstance(env, dict) and all(isinstance(v, str) for v in env.values()), name
        else:
            assert kind in {"http", "sse"}, name
            assert set(entry) <= {"type", "url", "headers"}, name
            assert entry["url"].startswith("https://"), name
            assert all(isinstance(v, str) for v in entry.get("headers", {}).values()), name


def test_mcp_json_jobsweep_entry() -> None:
    entry = load_json(MCP_JSON)["mcpServers"]["jobsweep"]
    assert entry["command"] == "uv"
    assert entry["args"] == ["run", "--directory", "jobsweep", "jobsweep-mcp"]
    assert (REPO_ROOT / entry["args"][2] / "pyproject.toml").exists()


def test_mcp_json_placeholders_have_defaults() -> None:
    """Every ${...} uses ${VAR:-...} so Claude Code parses the file even when the variable is unset."""
    raw = MCP_JSON.read_text(encoding="utf-8")
    found = PLACEHOLDER_RE.findall(raw)
    assert found, "expected ${VAR:-} placeholders for keys"
    for inner in found:
        m = SAFE_PLACEHOLDER_RE.match(inner)
        assert m, f"placeholder ${{{inner}}} must use the ${{VAR:-}} form"
        assert m.group(1) in ENV_VARS, f"{m.group(1)} is not in config.ENV_VARS"
        assert m.group(2) == "", "defaults must be empty (never a real value)"
    assert not re.search(r"(?<!\$)\$[A-Za-z_]", raw.replace("${", "")), "bare $VAR is not expanded"
    # Unset environment: expansion never fails and leaves no placeholders behind.
    for s in strings_in(load_json(MCP_JSON)):
        out = expand_like_claude_code(s, {})
        assert "${" not in out


def test_mcp_json_env_values_are_placeholders_only() -> None:
    for name, entry in load_json(MCP_JSON)["mcpServers"].items():
        for key, value in entry.get("env", {}).items():
            assert value == f"${{{key}:-}}", f"{name}.{key} must be ${{{key}:-}} (no hard-coded secrets)"
        for key, value in entry.get("headers", {}).items():
            assert PLACEHOLDER_RE.search(value), f"{name} header {key} must not hard-code a secret"


def test_mcp_json_uses_verified_launchers() -> None:
    servers = load_json(MCP_JSON)["mcpServers"]
    assert servers["ats-jobs"]["args"][:2] == ["-y", "github:benkrasin/ats-jobs-mcp"]
    assert "ats-jobs-mcp" not in servers["ats-jobs"]["args"]  # never the unrelated bare npm package
    assert servers["jobspy"]["args"] == ["mcp-jobspy@latest"]
    adzuna = servers["adzuna"]["args"]
    assert adzuna[0] == "--from" and adzuna[1].startswith("git+https://github.com/")  # not on PyPI
    assert "apis=indeed" in servers["hasdata-indeed"]["url"]
    apify_tools = servers["apify"]["url"].split("tools=", 1)[1].split(",")
    assert apify_tools[0] == "fetch-actor-details"
    assert {
        "aakash19/jobspy",
        "blackfalcondata/hiringcafe-scraper",
        "digital_influx/job-search-mcp",
        "piracema_labs/workday-jobs-monitor",
    } <= set(apify_tools)


# ---------------------------------------------------------------------------------------------------------
# .claude/settings.json


def test_claude_settings_enables_only_secret_free_servers() -> None:
    settings = load_json(CLAUDE_SETTINGS)
    enabled = settings["enabledMcpjsonServers"]
    assert len(enabled) == len(set(enabled))
    assert set(enabled) == NO_SECRET_SERVERS
    assert set(enabled) <= set(load_json(MCP_JSON)["mcpServers"])
    assert not settings.get("enableAllProjectMcpServers")


def test_claude_settings_permissions_are_narrow() -> None:
    settings = load_json(CLAUDE_SETTINGS)
    perms = settings.get("permissions", {})
    assert set(perms) <= {"allow", "deny", "ask"}
    assert "defaultMode" not in perms
    for rule in perms.get("allow", []):
        assert "*" not in rule and "(" not in rule, rule
        assert rule.startswith("mcp__jobsweep__"), rule
        assert rule.removeprefix("mcp__jobsweep__") in READ_ONLY_JOBSWEEP_TOOLS, rule


# ---------------------------------------------------------------------------------------------------------
# Claude Desktop config


def test_desktop_config_is_valid_and_has_no_secrets() -> None:
    data = load_json(DESKTOP_JSON)
    assert set(data) == {"mcpServers"}
    servers = data["mcpServers"]
    assert "jobsweep" in servers
    assert "/ABSOLUTE/PATH/TO/gradcafe_data/jobsweep" in servers["jobsweep"]["args"]
    for name, entry in servers.items():
        assert set(entry) <= {"command", "args", "env"}, name
        assert isinstance(entry["command"], str) and entry["command"], name
        assert all(isinstance(a, str) and a for a in entry.get("args", [])), name
        for key, value in entry.get("env", {}).items():
            assert value in ("", f"<{key}>"), f"{name}.{key} must be empty or the <{key}> placeholder"
    assert "ats-jobs-mcp" not in servers["ats-jobs"]["args"]


# ---------------------------------------------------------------------------------------------------------
# claude_code_setup.sh

bash = shutil.which("bash")
needs_bash = pytest.mark.skipif(bash is None, reason="bash not available")


def _script_env(tmp_path: Path, extra: dict[str, str] | None = None, path_prefix: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ENV_VARS and k not in {"BRAVE_API_KEY"}}
    env["HOME"] = str(tmp_path)  # keep ~/.config/jobsweep/.env out of the test
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    env.update(extra or {})
    return env


def _run_setup(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [bash, str(SETUP_SH), *args],  # type: ignore[list-item]
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _setup_known_servers() -> set[str]:
    m = re.search(r'^KNOWN_SERVERS="([^"]+)"', SETUP_SH.read_text(encoding="utf-8"), re.M)
    assert m
    return set(m.group(1).split())


def test_setup_script_is_executable_shell() -> None:
    text = SETUP_SH.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert SETUP_SH.stat().st_mode & stat.S_IXUSR
    assert "--dry-run" in text
    assert set(load_json(MCP_JSON)["mcpServers"]) <= _setup_known_servers()


@needs_bash
def test_setup_script_syntax() -> None:
    res = subprocess.run([bash, "-n", str(SETUP_SH)], capture_output=True, text=True, check=False)  # type: ignore[list-item]
    assert res.returncode == 0, res.stderr


@needs_bash
def test_setup_dry_run_masks_keys_and_skips_missing(tmp_path: Path) -> None:
    secrets = {
        "APIFY_TOKEN": "apify_api_FAKE0123456789",
        "HASDATA_API_KEY": "hd-FAKE-abcdef",
        "THEIRSTACK_API_KEY": "ts-FAKE-987654",
    }
    res = _run_setup(["--dry-run", "--no-env-file"], _script_env(tmp_path, secrets))
    assert res.returncode == 0, res.stderr
    out = res.stdout + res.stderr
    for value in secrets.values():
        assert value not in out
    assert "-s user -t http apify" in out and "Authorization: Bearer <APIFY_TOKEN>" in out
    assert "x-api-key: <HASDATA_API_KEY>" in out
    assert "Authorization: Bearer <THEIRSTACK_API_KEY>" in out
    assert "-s user -t http exa https://mcp.exa.ai/mcp" in out
    assert "skipped  adzuna" in out and "skipped  federal-compass" in out and "skipped  brave-search" in out
    assert "dry run:" in out


@needs_bash
def test_setup_dry_run_without_keys_skips_metered_unless_oauth(tmp_path: Path) -> None:
    res = _run_setup(["--dry-run", "--no-env-file"], _script_env(tmp_path))
    assert res.returncode == 0, res.stderr
    assert "skipped  apify" in res.stdout and "skipped  theirstack" in res.stdout
    res = _run_setup(["--dry-run", "--no-env-file", "--oauth", "--only", "apify"], _script_env(tmp_path))
    assert res.returncode == 0, res.stderr
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("[dry-run]")]
    assert len(lines) == 1 and "apify" in lines[0] and "Authorization" not in lines[0]


@needs_bash
def test_setup_rejects_bad_scope(tmp_path: Path) -> None:
    res = _run_setup(["--scope", "global", "--dry-run"], _script_env(tmp_path))
    assert res.returncode == 2


@needs_bash
def test_setup_print_desktop_config_fills_path(tmp_path: Path) -> None:
    res = _run_setup(["--print-desktop-config"], _script_env(tmp_path))
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert str(JOBSWEEP_DIR) in data["mcpServers"]["jobsweep"]["args"]


FAKE_CLAUDE = """#!/usr/bin/env bash
# fake claude CLI: logs argv (one arg per line, records separated by ---), simulates existing servers
printf '%s\\n' "$@" >> "$FAKE_CLAUDE_LOG"
echo --- >> "$FAKE_CLAUDE_LOG"
if [ "$1 $2" = "mcp add" ]; then
  for a in "$@"; do
    case ",$FAKE_EXISTING," in *",$a,"*) echo "MCP server $a already exists in user config" >&2; exit 1 ;; esac
    if [ "$a" = "$FAKE_FAIL" ]; then echo "boom for $a" >&2; exit 1; fi
  done
fi
exit 0
"""


def _fake_claude(tmp_path: Path) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "claude"
    exe.write_text(FAKE_CLAUDE, encoding="utf-8")
    exe.chmod(0o755)
    return bindir, tmp_path / "claude.log"


def _logged_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    calls, cur = [], []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "---":
            calls.append(cur)
            cur = []
        else:
            cur.append(line)
    return calls


def _added_name(call: list[str]) -> str:
    """Server name in a logged ``mcp add -s SCOPE [-t TRANSPORT] NAME ...`` call."""
    i = 2
    while call[i] in ("-s", "-t"):
        i += 2
    return call[i]


@needs_bash
def test_setup_runs_claude_and_is_idempotent(tmp_path: Path) -> None:
    bindir, log = _fake_claude(tmp_path)
    env_file = tmp_path / "keys.env"
    env_file.write_text(
        "# comment\n"
        'export ADZUNA_APP_ID="adz-id-1234"\n'
        "ADZUNA_APP_KEY='adz key 5678'\n"
        "HASDATA_API_KEY=hd-key-9999 # trailing comment\n",
        encoding="utf-8",
    )
    env = _script_env(
        tmp_path,
        {"FAKE_CLAUDE_LOG": str(log), "FAKE_EXISTING": "exa", "HASDATA_API_KEY": "from-env-wins"},
        path_prefix=bindir,
    )
    res = _run_setup(
        ["--no-env-file", "--env-file", str(env_file), "--only", "exa,adzuna,hasdata-indeed"], env
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "exists   exa" in res.stdout
    calls = {_added_name(c): c for c in _logged_calls(log) if c[:2] == ["mcp", "add"]}
    if shutil.which("uvx"):
        adz = calls["adzuna"]
        assert adz[:4] == ["mcp", "add", "-s", "user"]
        assert "ADZUNA_APP_ID=adz-id-1234" in adz and "ADZUNA_APP_KEY=adz key 5678" in adz
        assert adz[adz.index("--") + 1 :][:2] == ["uvx", "--from"]
    hd = calls["hasdata-indeed"]
    assert hd[-2:] == ["-H", "x-api-key: from-env-wins"]  # the real environment beats .env files
    for secret in ("adz-id-1234", "adz key 5678", "from-env-wins"):
        assert secret not in res.stdout + res.stderr

    # --force removes before re-adding; a failing add makes the script exit non-zero.
    log.unlink()
    env["FAKE_EXISTING"] = ""
    env["FAKE_FAIL"] = "exa"
    res = _run_setup(["--no-env-file", "--force", "--only", "exa,pipeworx-usajobs"], env)
    assert res.returncode == 1
    assert "FAILED   exa" in res.stderr and "added    pipeworx-usajobs" in res.stdout
    logged = _logged_calls(log)
    assert ["mcp", "remove", "-s", "user", "exa"] in logged


# ---------------------------------------------------------------------------------------------------------
# SKILL.md


def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    assert m, "SKILL.md must start with YAML frontmatter between --- lines"
    meta = yaml.safe_load(m.group(1))
    assert isinstance(meta, dict)
    return meta, m.group(2)


def test_skill_frontmatter() -> None:
    meta, body = _frontmatter(SKILL_MD)
    assert meta["name"] == "jobsweep" == SKILL_MD.parent.name
    assert re.fullmatch(r"[a-z0-9-]{1,64}", meta["name"])
    desc = meta["description"]
    assert isinstance(desc, str) and 50 < len(desc) <= 1024
    assert "job sweep" in desc.lower()
    assert set(meta) <= {"name", "description", "allowed-tools", "license", "metadata"}
    assert body.strip()


def test_skill_covers_tools_rules_and_connectors() -> None:
    _, body = _frontmatter(SKILL_MD)
    for needle in (
        "mcp__Indeed__search_jobs",
        "mcp__Indeed__get_job_details",
        "mcp__Indeed__get_company_data",
        "mcp__Indeed__get_resume",
        "mcp__Jobs_and_Careers__job-search",
        "mcp__Workopia__job_tool",
        "import_jobs",
        "jobsweep import",
        "digest",
        "Never apply",
        "Entry-level first",
        "links",
    ):
        assert needle in body, needle
    for tool in ALL_JOBSWEEP_TOOLS:
        assert f"{tool}(" in body, tool


def test_skill_query_plan_covers_every_active_lane() -> None:
    if not LANES_YAML.exists():
        pytest.skip("catalog/lanes.yaml not present")
    data = yaml.safe_load(LANES_YAML.read_text(encoding="utf-8"))
    _, body = _frontmatter(SKILL_MD)
    missing = [
        lane["id"]
        for group in data["groups"]
        if group.get("active", True)
        for lane in group["lanes"]
        if lane.get("active", True) and lane["id"] not in body
    ]
    assert not missing, f"lanes missing from the SKILL.md plan summary: {missing}"


# ---------------------------------------------------------------------------------------------------------
# .env.example and docs


def _env_example_assignments() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            assert m.group(1) not in out, f"duplicate {m.group(1)}"
            out[m.group(1)] = m.group(2)
    return out


def test_env_example_lists_exactly_config_env_vars() -> None:
    assigned = _env_example_assignments()
    assert list(assigned) and set(assigned) == set(ENV_VARS)
    assert all(v == "" for v in assigned.values()), "example values must stay empty"


def test_env_example_comments_every_var() -> None:
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^[A-Z_][A-Z0-9_]*=", line):
            # a comment directly above (possibly shared by a pair such as ADZUNA_APP_ID/KEY)
            j = i - 1
            while j >= 0 and re.match(r"^[A-Z_][A-Z0-9_]*=", lines[j]):
                j -= 1
            assert j >= 0 and lines[j].startswith("#"), f"no comment above {line}"


def test_integration_docs_exist() -> None:
    for name in (
        "claude_ai_connectors.md",
        "project_instructions.md",
        "scheduled_task_prompt.md",
        "claude_desktop_config.json",
        "claude_code_setup.sh",
    ):
        path = INTEGRATIONS / name
        assert path.exists() and path.stat().st_size > 200, name
    sched = (INTEGRATIONS / "scheduled_task_prompt.md").read_text(encoding="utf-8")
    assert "7:40" in sched and "America/New_York" in sched and "Indeed" in sched
    proj = (INTEGRATIONS / "project_instructions.md").read_text(encoding="utf-8")
    assert "Job sweeps" in proj
    for heading in ("## My lanes", "## Preferred locations", "## How to run a sweep", "## Output format"):
        assert heading in proj, heading


def test_connectors_doc_sections_and_rows() -> None:
    doc = CONNECTORS_MD.read_text(encoding="utf-8")
    for heading in (
        "## Connector table",
        "## claude.ai",
        "## Claude Desktop",
        "## Claude Code",
        "## Chrome",
        "## Projects",
        "## Scheduled tasks",
        "## GitHub Actions secrets",
        "## Troubleshooting",
    ):
        assert heading in doc, heading
    for name in (
        "Indeed",
        "Jobs and Careers",
        "Workopia",
        "Pipeworx USAJOBS",
        "JobSpy MCP",
        "aRustyDev/mcp-jobspy",
        "borgius/jobspy-mcp-server",
        "Apify MCP",
        "TheirStack",
        "HasData Indeed",
        "benkrasin/ats-jobs-mcp",
        "eosphor/ats-jobs-mcp",
        "ynsz-66/mcp-linkedin",
        "administrativetrick/linkedin-mcp",
        "federal-compass-mcp",
        "usajobs-monitor",
        "Adzuna MCP",
        "Exa",
        "Firecrawl",
        "Playwright MCP",
        "Brave Search",
        "Dreamwork",
        "career-ops",
        "proficiently",
        "claude-job-hunt",
        "Claude in Chrome",
        "NYC Jobs open data",
    ):
        assert name in doc, name
    secrets_section = doc.split("## GitHub Actions secrets", 1)[1].split("\n## ", 1)[0]
    for var in CREDENTIAL_VARS:
        assert f"`{var}`" in secrets_section, var
    for server in load_json(MCP_JSON)["mcpServers"]:
        assert f"`{server}`" in doc or server in doc, server


def test_no_hard_coded_secrets_in_integration_files() -> None:
    pattern = re.compile(r"(apify_api_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{16,}|Bearer [A-Za-z0-9._-]{16,})")
    for path in (MCP_JSON, CLAUDE_SETTINGS, DESKTOP_JSON, SETUP_SH, ENV_EXAMPLE, SKILL_MD, CONNECTORS_MD):
        assert not pattern.search(path.read_text(encoding="utf-8")), path


# ---------------------------------------------------------------------------------------------------------
# integration with other components (skip while those modules are missing)


def _example_tables(text: str) -> list[str]:
    """Markdown tables whose header row has a Title column."""
    tables, cur = [], []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|"):
            cur.append(s)
        else:
            if cur:
                tables.append("\n".join(cur))
            cur = []
    if cur:
        tables.append("\n".join(cur))
    return [t for t in tables if re.match(r"^\|\s*#?\s*\|?\s*Title\s*\|", t)]


def test_documented_output_tables_are_importable() -> None:
    try:
        from jobsweep.sources.manual_import import ImportSource
    except ImportError:
        pytest.skip("jobsweep.sources.manual_import not available")
    for path in (
        INTEGRATIONS / "scheduled_task_prompt.md",
        INTEGRATIONS / "project_instructions.md",
        SKILL_MD,
    ):
        tables = _example_tables(path.read_text(encoding="utf-8"))
        assert tables, f"no example table with a Title column in {path.name}"
        for table in tables:
            jobs = ImportSource.parse(table, origin="docs-example")
            assert jobs, f"{path.name}: example table did not import"
            assert all(j.title and j.url.startswith("http") and j.company for j in jobs)


def test_mcp_server_exposes_documented_tools() -> None:
    try:
        mod = importlib.import_module("jobsweep.mcp_server")
    except ImportError:
        pytest.skip("jobsweep.mcp_server not available yet")
    from mcp.server.fastmcp import FastMCP

    servers = [v for v in vars(mod).values() if isinstance(v, FastMCP)]
    if not servers:
        pytest.skip("no module-level FastMCP instance in jobsweep.mcp_server")
    tools = {t.name for t in asyncio.run(servers[0].list_tools())}
    assert ALL_JOBSWEEP_TOOLS <= tools, sorted(ALL_JOBSWEEP_TOOLS - tools)
    allowed = load_json(CLAUDE_SETTINGS)["permissions"]["allow"]
    assert {a.removeprefix("mcp__jobsweep__") for a in allowed} <= tools
