# Daily metered searches

The daily GitHub Actions sweep includes TheirStack, HasData Indeed, and Apify.
They use a separate budgeted path after the ordinary sources finish. Manual
`include_metered` runs and explicit selections of these providers use the same
daily allowance. A rerun cannot buy another batch on the same UTC day.

| Provider | Daily allowance | Cost ceiling | Additional gate |
| --- | --- | --- | --- |
| TheirStack | 1 rotating query, up to 6 returned rows | 6 API credits/day; 200 reserved credits/calendar month | Current API credit balance must cover the request |
| HasData Indeed | Up to 10 rotating queries, one page each; automatically reduced to 3 on a 1,000-credit account | 10 credits/request; 4,000 reserved credits/calendar month | Actual account total and remaining credits are read before each daily batch |
| Apify Hiring.Cafe | 1 rotating query, up to 50 results | $0.08 actor charge/run; $2.50 reserved actor charges/calendar month | Account hard limit must already fit inside included plan credit; remaining credit must cover the run plus $0.10 overhead headroom |

A 5,000-credit HasData account allows 10 requests/day, at most 3,100 credits in
a 31-day month. The connected account observed during setup was a 1,000-credit
trial, so it receives 3/day instead. TheirStack was a 200-credit free account and
Apify a $5 free account. These checks do not assume a trial or one-time credit
balance renews. No plan is upgraded and no account spending setting is changed.

Queries rotate through the active taxonomy and alternate New York and US remote
searches. Provider location filters are supplemented by the board's location
assessment. The TheirStack adapter supports US/remote filters; its New York
query can return other US locations, which the board then screens.

Before sending paid requests, the workflow reads account balances without logging
raw account responses, creates reservations in `metered-budget.json`, and pushes
that ledger to the state branch. A failed save means paid searches are skipped.
Reservations are not refunded on failure because a timed-out request may still
have been charged. HasData and TheirStack requests are not automatically retried.
The TheirStack cap counts raw returned rows, including malformed/duplicate rows.

The Actions run summary records allowed searches and reasons for skipped
providers. `metered-report.json` contains results and source errors. Missing keys,
unknown account response formats, exhausted credit, and unsafe Apify spending
limits fail closed. Apify's actor charge cap does not cover all possible platform
and dataset costs, hence the separate existing account limit check.

The ledger covers this workflow's reservations. Other tools using the same API
accounts are detected by the next balance check but are not controlled by it.
