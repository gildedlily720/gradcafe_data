#!/usr/bin/env bash
# Register the jobsweep MCP server and the job-search MCP servers with Claude Code.
#
# Default scope is "user" (available in every directory). Inside this repo the project-scope entries of
# the repo-root .mcp.json take precedence over user-scope servers with the same name (local > project >
# user); use --scope local to make the keyed variants win inside this checkout only.
#
# Idempotent: a server that already exists in the chosen scope is left alone (--force replaces it).
# Keys come from the environment first, then from jobsweep/.env (never overriding the environment).
# Servers whose keys are missing are skipped and listed at the end. Secrets are masked in all output.
#
# Usage: jobsweep/integrations/claude_code_setup.sh [options]
#   --dry-run                 print the `claude mcp add` commands without running them
#   --scope user|local|project
#   --force                   remove and re-add servers that already exist in that scope
#   --oauth                   add apify / theirstack / hasdata-indeed without a key; sign in later with /mcp
#   --only a,b,c              only these server names
#   --no-env-file             do not read jobsweep/.env
#   --print-desktop-config    print integrations/claude_desktop_config.json with this checkout's path, then exit
#   -h, --help
#
# Works with bash 3.2 (macOS /bin/bash) and newer.
set -eo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
JOBSWEEP_DIR=$(dirname "$SCRIPT_DIR")
REPO_ROOT=$(dirname "$JOBSWEEP_DIR")

APIFY_URL='https://mcp.apify.com/?tools=fetch-actor-details,aakash19/jobspy,blackfalcondata/hiringcafe-scraper,digital_influx/job-search-mcp,piracema_labs/workday-jobs-monitor'
THEIRSTACK_URL='https://api.theirstack.com/mcp'
HASDATA_URL='https://mcp.hasdata.com/mcp?apis=indeed'
PIPEWORX_URL='https://gateway.pipeworx.io/usajobs/mcp'
EXA_URL='https://mcp.exa.ai/mcp'
# Not on PyPI: the Python Adzuna server is installed from GitHub, pinned to the reviewed commit.
ADZUNA_SRC='git+https://github.com/folathecoder/adzuna-job-search-mcp@e00a5956bc07524a6bddadb1d79db731ce107461'
DESKTOP_PLACEHOLDER='/ABSOLUTE/PATH/TO/gradcafe_data'

# Values masked in every line this script prints.
SECRET_VARS="USAJOBS_API_KEY USAJOBS_EMAIL PIPEWORX_API_KEY ADZUNA_APP_ID ADZUNA_APP_KEY THEIRSTACK_API_KEY HASDATA_API_KEY APIFY_TOKEN SOCRATA_APP_TOKEN BRAVE_API_KEY DREAMWORK_API_KEY"

DRY_RUN=0
FORCE=0
OAUTH=0
USE_ENV_FILE=1
SCOPE=user
ONLY=""
ADDED=()
EXISTING=()
SKIPPED=()
FAILED=()

usage() { sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --force) FORCE=1 ;;
    --oauth) OAUTH=1 ;;
    --no-env-file) USE_ENV_FILE=0 ;;
    --scope)
      [ $# -ge 2 ] || { echo "--scope needs a value" >&2; exit 2; }
      SCOPE=$2
      shift
      ;;
    --scope=*) SCOPE=${1#--scope=} ;;
    --only)
      [ $# -ge 2 ] || { echo "--only needs a value" >&2; exit 2; }
      ONLY=$2
      shift
      ;;
    --only=*) ONLY=${1#--only=} ;;
    --print-desktop-config)
      sed "s|$DESKTOP_PLACEHOLDER|$REPO_ROOT|g" "$SCRIPT_DIR/claude_desktop_config.json"
      exit 0
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1 (see --help)" >&2
      exit 2
      ;;
  esac
  shift
done

case "$SCOPE" in
  user | local | project) ;;
  *)
    echo "--scope must be user, local or project (got '$SCOPE')" >&2
    exit 2
    ;;
esac

# ---- helpers ------------------------------------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

trim() {
  local s=$1
  s=${s#"${s%%[![:space:]]*}"}
  s=${s%"${s##*[![:space:]]}"}
  printf '%s' "$s"
}

# KEY=VALUE lines (optional "export ", quotes, trailing " # comment"); never overrides the environment.
load_env_file() {
  local file=$1 line key value first
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line=$(trim "$line")
    case "$line" in "" | \#*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    case "$line" in "export "*) line=${line#export } ;; esac
    key=$(trim "${line%%=*}")
    value=$(trim "${line#*=}")
    if ! [[ $key =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      continue
    fi
    first=${value:0:1}
    if [ ${#value} -ge 2 ] && { [ "$first" = '"' ] || [ "$first" = "'" ]; } && [ "${value: -1}" = "$first" ]; then
      value=${value:1:${#value}-2}
    else
      case "$value" in *" #"*) value=$(trim "${value%% #*}") ;; esac
    fi
    if [ -n "$value" ] && [ -z "${!key}" ]; then
      export "$key=$value"
    fi
  done <"$file"
  return 0
}

mask() {
  local s=$1 var val rep
  for var in $SECRET_VARS; do
    val=${!var}
    if [ ${#val} -ge 4 ]; then
      rep="<$var>"
      s=${s//"$val"/$rep}
    fi
  done
  printf '%s' "$s"
}

# Shell-quote for display (after masking).
show() {
  local a out=""
  for a in "$@"; do
    a=$(mask "$a")
    if [[ $a =~ ^[A-Za-z0-9_./:@%+=,-]+$ ]]; then
      out="$out $a"
    else
      out="$out '$(printf '%s' "$a" | sed "s/'/'\\\\''/g")'"
    fi
  done
  printf '%s' "${out# }"
}

selected() {
  [ -z "$ONLY" ] && return 0
  case ",$ONLY," in *",$1,"*) return 0 ;; esac
  return 1
}

# register NAME ARGS...  ->  claude mcp add -s SCOPE ARGS...   (ARGS include the server name)
register() {
  local name=$1 out
  shift
  selected "$name" || return 0
  local cmd=(claude mcp add -s "$SCOPE" "$@")
  if [ "$DRY_RUN" = 1 ]; then
    printf '[dry-run] %s\n' "$(show "${cmd[@]}")"
    ADDED+=("$name")
    return 0
  fi
  if [ "$FORCE" = 1 ]; then
    claude mcp remove -s "$SCOPE" "$name" >/dev/null 2>&1 || true
  fi
  if out=$("${cmd[@]}" 2>&1); then
    printf 'added    %s\n' "$name"
    ADDED+=("$name")
  elif printf '%s' "$out" | grep -qi 'already exists'; then
    printf 'exists   %s (left unchanged; --force replaces it)\n' "$name"
    EXISTING+=("$name")
  else
    printf 'FAILED   %s: %s\n' "$name" "$(mask "$out")" >&2
    FAILED+=("$name")
  fi
  return 0
}

skip() {
  selected "$1" || return 0
  printf 'skipped  %s: %s\n' "$1" "$2"
  SKIPPED+=("$1 ($2)")
  return 0
}

node_major() {
  have node || {
    echo 0
    return 0
  }
  node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0
}

# ---- environment --------------------------------------------------------------------------------------

if [ "$USE_ENV_FILE" = 1 ]; then
  load_env_file "$JOBSWEEP_DIR/.env"
fi

if [ "$DRY_RUN" = 0 ] && ! have claude; then
  echo "claude (Claude Code CLI) not found on PATH. Install it, or run with --dry-run to see the commands." >&2
  exit 1
fi

# Run from the repo root so --scope local/project bind to this checkout.
cd "$REPO_ROOT"
echo "Registering job-search MCP servers (scope: $SCOPE$([ "$DRY_RUN" = 1 ] && echo ', dry run'))"

# ---- 1. jobsweep (local stdio server; reads jobsweep/.env itself) ----------------------------------------
if have uv; then
  register jobsweep jobsweep -- uv run --directory "$JOBSWEEP_DIR" jobsweep-mcp
elif [ -x "$REPO_ROOT/.venv/bin/jobsweep-mcp" ]; then
  register jobsweep jobsweep -- "$REPO_ROOT/.venv/bin/jobsweep-mcp"
else
  skip jobsweep "needs uv (https://docs.astral.sh/uv/) or an installed jobsweep-mcp"
fi

# ---- 2. Pipeworx USAJOBS (remote, keyless; optional key raises the 50 calls/day quota) -------------------
if [ -n "$PIPEWORX_API_KEY" ]; then
  register pipeworx-usajobs -t http pipeworx-usajobs "$PIPEWORX_URL?_apiKey=$PIPEWORX_API_KEY"
else
  register pipeworx-usajobs -t http pipeworx-usajobs "$PIPEWORX_URL"
fi

# ---- 3. Exa web search (remote, keyless) -------------------------------------------------------------------
register exa -t http exa "$EXA_URL"

# ---- 4. JobSpy (uvx, keyless) ------------------------------------------------------------------------------
if have uvx; then
  register jobspy jobspy -- uvx mcp-jobspy@latest
else
  skip jobspy "needs uvx (install uv)"
fi

# ---- 5. ATS jobs (Greenhouse/Lever/Ashby; APIFY_TOKEN only for search_jobs_all_companies) ------------------
if have npx; then
  if [ -n "$APIFY_TOKEN" ]; then
    register ats-jobs ats-jobs -e "APIFY_TOKEN=$APIFY_TOKEN" -- npx -y github:benkrasin/ats-jobs-mcp mcp
  else
    register ats-jobs ats-jobs -- npx -y github:benkrasin/ats-jobs-mcp mcp
  fi
else
  skip ats-jobs "needs npx (Node.js 18+)"
fi

# ---- 6. Playwright browser (keyless) -----------------------------------------------------------------------
if have npx; then
  register playwright playwright -- npx -y @playwright/mcp@latest
else
  skip playwright "needs npx (Node.js 18+)"
fi

# ---- 7-9. Metered remote servers: API key header, or OAuth with --oauth ------------------------------------
if [ -n "$APIFY_TOKEN" ]; then
  register apify -t http apify "$APIFY_URL" -H "Authorization: Bearer $APIFY_TOKEN"
elif [ "$OAUTH" = 1 ]; then
  register apify -t http apify "$APIFY_URL"
else
  skip apify "APIFY_TOKEN not set (or pass --oauth and sign in with /mcp)"
fi

if [ -n "$THEIRSTACK_API_KEY" ]; then
  register theirstack -t http theirstack "$THEIRSTACK_URL" -H "Authorization: Bearer $THEIRSTACK_API_KEY"
elif [ "$OAUTH" = 1 ]; then
  register theirstack -t http theirstack "$THEIRSTACK_URL"
else
  skip theirstack "THEIRSTACK_API_KEY not set (or pass --oauth and sign in with /mcp)"
fi

if [ -n "$HASDATA_API_KEY" ]; then
  register hasdata-indeed -t http hasdata-indeed "$HASDATA_URL" -H "x-api-key: $HASDATA_API_KEY"
elif [ "$OAUTH" = 1 ]; then
  register hasdata-indeed -t http hasdata-indeed "$HASDATA_URL"
else
  skip hasdata-indeed "HASDATA_API_KEY not set (or pass --oauth and sign in with /mcp)"
fi

# ---- 10. Adzuna (Python server from GitHub; needs app id + key) ---------------------------------------------
if [ -z "$ADZUNA_APP_ID" ] || [ -z "$ADZUNA_APP_KEY" ]; then
  skip adzuna "ADZUNA_APP_ID / ADZUNA_APP_KEY not set"
elif ! have uvx; then
  skip adzuna "needs uvx (install uv)"
else
  register adzuna adzuna -e "ADZUNA_APP_ID=$ADZUNA_APP_ID" -e "ADZUNA_APP_KEY=$ADZUNA_APP_KEY" \
    -- uvx --from "$ADZUNA_SRC" adzuna-mcp
fi

# ---- 11. federal-compass (USAJOBS API; Node 22+) ------------------------------------------------------------
if [ -z "$USAJOBS_API_KEY" ] || [ -z "$USAJOBS_EMAIL" ]; then
  skip federal-compass "USAJOBS_API_KEY / USAJOBS_EMAIL not set"
elif [ "$(node_major)" -lt 22 ]; then
  skip federal-compass "needs Node.js 22+"
else
  register federal-compass federal-compass -e "USAJOBS_API_KEY=$USAJOBS_API_KEY" -e "USAJOBS_EMAIL=$USAJOBS_EMAIL" \
    -- npx -y federal-compass-mcp@latest
fi

# ---- 12. Brave Search (official package; @modelcontextprotocol/server-brave-search is deprecated) ----------
if [ -z "$BRAVE_API_KEY" ]; then
  skip brave-search "BRAVE_API_KEY not set"
elif ! have npx; then
  skip brave-search "needs npx (Node.js 18+)"
else
  register brave-search brave-search -e "BRAVE_API_KEY=$BRAVE_API_KEY" \
    -- npx -y @brave/brave-search-mcp-server --transport stdio
fi

# ---- summary ------------------------------------------------------------------------------------------------
echo
if [ "$DRY_RUN" = 1 ]; then
  echo "dry run: ${#ADDED[@]} server(s) would be added, ${#SKIPPED[@]} skipped"
else
  echo "done: ${#ADDED[@]} added, ${#EXISTING[@]} already present, ${#SKIPPED[@]} skipped, ${#FAILED[@]} failed"
fi
if [ ${#SKIPPED[@]} -gt 0 ]; then
  echo "Set the missing keys in your shell or jobsweep/.env (see jobsweep/.env.example) and re-run to add them."
fi
echo "Next: 'claude mcp list' to check health; inside Claude Code run /mcp to sign in to OAuth servers."
echo "Docs: jobsweep/docs/CONNECTORS.md"
[ ${#FAILED[@]} -eq 0 ]
