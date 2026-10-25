# claude.ai connectors for job sweeps

Connectors you add on claude.ai (web) work in claude.ai chats, Projects and scheduled tasks. They also
appear in **Claude Desktop** and in **Claude Code** sessions signed in with the same claude.ai account,
where tools show up as `mcp__<Connector>__<tool>`, for example `mcp__Indeed__search_jobs`. Results from
connectors reach the local jobsweep database only when you import them with the MCP tool `import_jobs`
or `jobsweep import` (see `.claude/skills/jobsweep/SKILL.md`).

Probe status is as of 2026-10-25. The full table is in `jobsweep/docs/CONNECTORS.md`.

## Directory connectors (one click)

Open a chat, click **Search and tools**, then **+ Add connectors** (or go to Settings > Connectors > Browse
connectors), search for the name, and click **Connect**.

| Connector | Sign-in | What it gives the sweep | Notes |
|---|---|---|---|
| **Indeed** (beta) | Sign in with Indeed | Job Search, Job Detail, Get Company Data, Get Resume | Available only as a Claude connector. There's no URL for Claude Code config files, but it appears in Claude Code through your claude.ai login. Locations: US cities, "remote", and London with country code GB. |
| **Jobs and Careers** | already connected | `job-search` (US only; entry-level filter `seniority: ["ENTRY"]`) | The tool can also apply, so only use it to search. |
| **Workopia** | already connected | `job_tool` search/refine, `job_detail_tool` | Its save/tailor/cover-letter tools aren't part of sweeps. |
| **Exa** | follow the Connect prompt | web search over career pages (think tanks, IGOs, law firms) | Also usable from Claude Code: `claude mcp add --transport http exa https://mcp.exa.ai/mcp` |
| **Firecrawl** | sign in, choose a team | read and crawl a specific careers page | Uses Firecrawl credits |

## Custom connectors (remote MCP URLs)

Go to **Settings > Connectors > Add custom connector**. Enter a name and the URL, and choose an auth option:
**Sign in now**, **Sign in when needed** or **No sign in**. Request headers are a beta option; their value
is sent verbatim, so for bearer tokens include the word "Bearer ".

Rules:
- The server must be reachable from Anthropic's cloud. Local servers (localhost, 127.0.0.1) don't work
  here; use Claude Code or Claude Desktop for those.
- The **Free plan** allows **one** custom connector. Pro and Max allow more.
- On **Team and Enterprise**, an owner adds organization connectors in **Organization settings >
  Connectors**. Members then connect them.

| Name | URL | Auth | Cost | Probe 2026-10-25 |
|---|---|---|---|---|
| Pipeworx USAJOBS | `https://gateway.pipeworx.io/usajobs/mcp` | No sign in. For your own quota, use the URL `...?_apiKey=<auth_key>:<email>` | free, about 50 calls/day | initialize + tools/list OK without auth (5 USAJOBS tools + about 42 shared meta tools) |
| Apify (job actors) | `https://mcp.apify.com/?tools=fetch-actor-details,aakash19/jobspy,blackfalcondata/hiringcafe-scraper,digital_influx/job-search-mcp,piracema_labs/workday-jobs-monitor` | Sign in now (OAuth), or request header `Authorization: Bearer <APIFY_TOKEN>` | pay per result | 401 without a token; OAuth metadata published |
| TheirStack | `https://api.theirstack.com/mcp` | Sign in now (OAuth), or header `Authorization: Bearer <THEIRSTACK_API_KEY>` | API credits | 401 without a token; OAuth metadata published |
| HasData Indeed | `https://mcp.hasdata.com/mcp?apis=indeed` | Sign in now (OAuth), or header `x-api-key: <HASDATA_API_KEY>` (no "Bearer") | free 1,000 credits = about 100 calls/month | initialize + tools/list OK; tool calls return 401 without a key |

On the Free plan, pick **Pipeworx USAJOBS** as your one custom connector: it's free and keyless and covers
the federal Pathways lane. The local jobsweep sweep already covers USAJOBS, NYC Jobs, and HasData, Apify
and TheirStack through their APIs.

## Step by step

1. **Indeed:**
   1. Open a chat, then **Search and tools > + Add connectors**, search "Indeed", click **Connect**, and sign
      in with your Indeed account.
   2. In the chat's **Search and tools** menu, make sure Indeed is toggled on.
   3. Test it with: "Search Indeed for political risk analyst jobs in New York, NY".
2. **Pipeworx USAJOBS:**
   1. Go to **Settings > Connectors > Add custom connector**.
   2. Name it "Pipeworx USAJOBS", use the URL above, and choose **No sign in**.
   3. Test it with: "Use Pipeworx USAJOBS to search for pathways recent graduate postings at grade 05-09".
3. **Apify:**
   1. Add a custom connector named "Apify jobs" with the URL above.
   2. Choose **Sign in now**, approve in the Apify console, and keep the default `full_api_access` scope.
   3. Every actor run is billed to your Apify account, so tell Claude to ask before running actors.
4. **TheirStack:** add a custom connector named "TheirStack" with **Sign in now**. Searches use API credits.
5. **HasData Indeed:** add a custom connector with **Sign in now**, or use the request header
   `x-api-key` set to your HasData key.
6. **Exa / Firecrawl:** Browse connectors, then Connect (Firecrawl asks you to pick a team).
7. **Claude in Chrome** (paid plans, Chrome only):
   1. Install the extension.
   2. In claude.ai go to **Settings > Customize > Connectors > Claude in Chrome > Configure**.
   3. Use it for career sites that have no API (iCIMS, Taleo and custom pages): run
      `jobsweep employers --manual` to get the "check by hand" list with its careers URLs.
8. **Enable per chat:** connectors are switched on per conversation in **Search and tools**. Projects and
   scheduled tasks use the connectors you enable for them.

Paste-ready texts: `project_instructions.md` (Project "Job sweeps") and `scheduled_task_prompt.md` (daily
7:40am ET sweep).
