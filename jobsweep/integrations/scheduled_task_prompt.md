# Scheduled task: daily job sweep (7:40am ET)

**Setup** (claude.ai or Claude Desktop / Cowork):

1. Open **Scheduled**, choose **New task**, and name it **Daily job sweep**.
2. Set the schedule to **Daily** (or **Weekdays**) at **7:40 AM**, with the time zone set to
   America/New_York (Eastern Time). Scheduled tasks run remotely in the cloud, so your computer can be off.
3. Turn on these connectors for the task:
   - **Indeed** (required)
   - **Jobs and Careers** and **Workopia** (recommended)
   - **Pipeworx USAJOBS** (if you added it as a custom connector)
   - **Exa** (optional)
   - **Google Drive**, so it can keep the running doc
4. Paste everything below the line as the task prompt. Run it once by hand to check the doc gets created.

The task only uses connectors. It can't reach the local jobsweep database. The running doc's tables are
in a format the local tool can import later:
`uv run --directory jobsweep jobsweep import sweep-log.md --origin scheduled-sweep`.

---8<--- paste below this line ---8<---

Run my daily job sweep and append the results to my running doc. You must not apply to, save, or message
anything on my behalf. Only search, read and write the doc.

**Who I am:** an early-career candidate (0-3 years of experience). I want analyst, associate, assistant,
coordinator, graduate-program, fellowship and other entry-level roles. My preferred locations are New
York, then Washington DC, then remote (US), then London.

**My lanes:**
- Specialty insurance: political risk, political violence and terrorism, war risk, K&R, special risks,
  trade credit and surety, transactional/M&A, financial lines, fine art and specie, aviation, marine, and
  reinsurance broking, plus insurance fraud (SIU).
- Intelligence and security: strategic, threat and protective intelligence, travel risk, GSOC/watch
  officer, corporate security, OSINT and counterterrorism.
- Investigations and due diligence.
- Geopolitical and political risk.
- Sanctions, export controls and financial crime.
- Trust and safety, information integrity, and AI policy.
- Policy research, think tanks and international organizations.
- Strategic communications.
- NYC/NYS government and district attorney offices.
- Federal Pathways Recent Graduates, GS-5 to GS-9.
- Graduate, analyst and fellowship programs.

## Steps

1. **Find or create the running doc.** In Google Drive, look for a Google Doc named exactly
   "Job sweep log". Create it if it doesn't exist. Read its recent sections so you know which jobs I've
   already seen. Treat a job as seen if its link, or its title plus company plus city, appears in the doc.
2. **Indeed connector.**
   - Run `search_jobs` for each of these queries in "New York, NY" and "Washington, DC" (country_code US):
     political risk analyst, intelligence analyst, threat intelligence analyst, due diligence analyst,
     sanctions analyst and trust and safety analyst.
   - Run the first three with location "remote" too.
   - In "London" (country_code GB), run graduate underwriter and political risk analyst.
   - Then add today's rotation, 4 queries in New York and 2 in Washington:

     | Day | Queries |
     |---|---|
     | Monday | political risk underwriter; underwriting assistant specialty; reinsurance broker graduate; K&R underwriter; trade credit underwriter; SIU investigator |
     | Tuesday | GSOC analyst; protective intelligence analyst; OSINT analyst; counterterrorism analyst; travel risk analyst; corporate security analyst |
     | Wednesday | investigative research analyst; corporate investigations analyst; export compliance analyst; financial intelligence analyst; AML analyst entry level; blockchain investigator |
     | Thursday | geopolitical analyst; country risk analyst; national security research assistant; foreign policy research assistant; think tank research associate; legislative analyst |
     | Friday | policy enforcement analyst; disinformation analyst; strategic communications associate; government affairs associate; investigative analyst district attorney; emergency management specialist |
     | Saturday and Sunday | graduate programme insurance; analyst program; policy fellowship; junior fellow; rotational program analyst; research fellowship recent graduate |

   - Use `get_job_details` only for hits whose level or requirements are unclear (at most 8 calls).
3. **Jobs and Careers connector**, if enabled.
   - Make up to 4 `job-search` calls with `seniority` ["ENTRY"], covering today's lanes with title
     variants in `keywords`, in New York, NY and Washington, DC.
   - Search only.
4. **Workopia connector**, if enabled. Make up to 4 `job_tool` searches (action "search", job_title + city,
   posted_within_days 3) for today's lanes in New York and Washington, and one in London.
5. **Pipeworx USAJOBS**, if enabled. Make at most 3 `search` calls for Pathways or recent-graduate postings,
   for example "pathways recent graduate", "foreign affairs" and "international trade", at grades 05-09.
6. **Filter.**
   - Keep postings that match a lane.
   - Drop anything already in the doc, internships, and clearly senior roles (Senior, Lead, Principal,
     Director, VP, Head of, or 5+ years required).
   - Note postings whose level is unclear rather than dropping them.
   - Never invent salaries or deadlines. Leave the cell empty when the source doesn't say.
7. **Write the doc.** Insert today's section at the top, under the doc title, newest first. Use exactly this
   format:

   ```
   ## YYYY-MM-DD (Day) - N new

   Sources: Indeed (n), Jobs and Careers (n), Workopia (n), USAJOBS (n). Failed or skipped: ...

   | # | Title | Company | Location | Posted | Pay | Lane | Fit | Source |
   |---|---|---|---|---|---|---|---|---|
   | 1 | [Political Risk Analyst](https://example.com/job/1) | Example Co | New York, NY | 2026-10-24 | $70,000-$80,000 | political-risk | entry | Indeed |
   ```

   - Use one row per job, entry-level first, then by lane group.
   - Link the title to the posting URL exactly as the connector returned it.
   - Fit is entry, unclear or stretch. Lane is a short lane name such as political-risk, gsoc-watch,
     sanctions or federal-pathways.
   - If nothing new turned up, still add the heading with "0 new" and the sources line.
   - If the Drive connector can only replace a whole file, rewrite the doc with today's section added at
     the top. Keep every earlier section unchanged.
8. **Reply** with a 3-5 line summary: the counts, the best 3 matches with links, and anything closing within
   7 days.
