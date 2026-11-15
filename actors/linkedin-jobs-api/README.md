# LinkedIn Jobs API

Public LinkedIn job search as rows you can work with. Give it a search phrase, get one
row per job with the full description, not just the title.

## Why it exists

Job boards answer a plain HTTP request with a wall. Measured on 2026-11-12 from a
Frankfurt datacenter address: a plain request to the guest search was refused, while a
real Chromium window on a virtual display read 60 job cards on the search page and an
8181 character description on a job page, with no sign-in. Many actors on the market
stop at the card, so you get a title, a company and a link.

## What it does

1. Opens the guest job search for your keywords, location and filters.
2. Walks the date windows (lenses) you allow, dedupes by job id.
3. Opens each job page and reads the description, seniority level, employment type,
   job function, industries, applicants count and the apply link.
4. Writes one row per job and charges only for a row that landed in the dataset.

## Depth, measured rather than promised

A guest session does not paginate. Measured on 2026-11-12 with one browser session:

* `start=` offsets do not work: start=0, 25, 50, 200 and 250 returned the same first
  cards.
* `sortBy=DD` returned exactly the same 60 ids as relevance.
* Each search page carries 60 unique job cards.
* The date filter is a lever: the past week window returned 60 ids with only 14 shared
  with the unfiltered set (union 106).

So the actor walks up to three date windows (unfiltered, past week, past 24 hours) and
dedupes. The honest ceiling is 180 jobs per search phrase, and more phrases mean more
jobs.

## Input

| Field | Default | Meaning |
|---|---|---|
| `keywords` | `python developer` | What you would type into the search. Required. |
| `location` | `United States` | A place name; empty searches globally. |
| `maxJobs` | 50 | Ceiling that protects your budget, 1 to 180. |
| `maxSearches` | 3 | Date windows to walk (lenses), 1 to 3. |
| `datePosted` | any | any, past24h, pastWeek, pastMonth. A chosen window is walked alone. |
| `experience` | any | internship, entry, associate, mid-senior, director, executive. |
| `remote` | any | onsite, hybrid, remote. |
| `includeDescription` | true | Open each job page. Turn it off for a fast list of cards. |

## Output

One row per job: `jobId`, `url`, `title`, `company`, `location`, `postedAt`,
`postedText`, `employmentType`, `seniorityLevel`, `jobFunction`, `industries`,
`applyUrl`, `applicants`, `description`, `descriptionCharacters`, `companyUrl`,
`fetchedAt`, `input`, `inputIndex`, `status`.

A row whose `status` is not `OK` carries none of those fields, only the reason, so a
free row is never the product.

## Price

Pay per delivered job: one event per row that reached the dataset. `chargedEvents` in
the run summary is the money actually taken, and `billable` on a row is only a claim
about the price list.

Before a batch is written the run asks the platform how many rows your remaining budget
still covers. A batch that does not fit is cut first, never in silence: every row past
the budget comes back free as `NOT_CHECKED_CAP` with a reason, and `chargedEvents` never
counts a row that was not charged. The same happens if the platform charges less than
it allowed - the counters follow the charge, not the batch.

Free, with a status and a reason in the row:

* `NOT_CHECKED_LIMIT` - beyond `maxJobs`;
* `NOT_CHECKED_TIME` - the run ran out of time;
* `NOT_CHECKED_BLOCKED` - a wall answered instead of the job;
* `NOT_CHECKED_ERROR` - the job page could not be read;
* `NOT_CHECKED_CAP` - your spending cap was reached;
* `NOT_CHECKED_BILLING` - the price could not be read, so nothing was sold;
* `NO_JOBS_FOUND` - the search itself carried no cards.

## What this actor does not do

* It does not sign in. A guest session carries 60 cards per window, so 180 jobs per
  phrase is the ceiling; going deeper needs an account, and using yours is not
  something this actor will do.
* It does not read recruiter profiles, contact details, or anything behind a login.
* It does not invent a salary: LinkedIn often does not print one on the guest page, so
  the row simply has no salary field.
* It does not promise every job on LinkedIn, only what the guest search returns for your
  phrase, location and filters.

## Notes

The browser runs headful under Xvfb because a headless window is refused where a real
one is served. That costs memory: 1024 MB is the default and the measured floor. The
run stops with `stopReason: wall` and stops charging if LinkedIn answers a challenge.
