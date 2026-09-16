# The dashboard

The dashboard is four pages under one sign-in, with three tabs in the
header on every page:

- **Workshops**, at `/`, is the overview: sessions over time across
  every workshop, a table of the workshops with a sparkline each, and
  the funnel across each collection.

- **Now**, at `/live`, is the live view: the sessions being done
  right now, grouped by workshop, updating as batches arrive.

- **History**, at `/sessions`, is every session the store holds,
  filtered by workshop, collection, host, frontend, status, labels
  and period, newest first, with the selection downloadable.

- A workshop's page, at `/workshops/<name>`, reached from the other
  three, carries its reports: outcomes, the trend, the funnel, time
  on each page, what nobody ran, and the checks.

Every page is rendered on the server from the same query functions
the [API](api.md) answers with, so a number on a page is the number
the API gives, and the pages need nothing fetched from anywhere but
the service. Filters and periods live in the URL, with the same
names as the API's query parameters, so a view can be linked to and
comes back the same. Times are shown in the browser's own clock, with
the UTC timestamp on hover.

## Signing in

The pages are entered with a token carrying the `dashboard` scope
([tokens](tokens.md)), by either of two routes:

- **The login form.** Paste the token once. It is exchanged for a
  session cookie and never appears in the address bar, the history,
  a referrer or a log.

- **A one-time link.** `https://analytics.example/?token=<token>`,
  for a link pasted into a classroom chat. The service validates the
  token, sets the cookie and redirects to the bare URL, so the token
  leaves the address bar and the history entry. The link is a
  credential while it circulates; issue the token for the course and
  let it expire.

The cookie holds a signed session value, not the token, and is
`HttpOnly`, `SameSite=Strict`, and `Secure` when the page is served
over HTTPS (also when a proxy in front says so with
`X-Forwarded-Proto`; `COOKIE_SECURE` forces it either way). It lasts
`SESSION_HOURS` (12 by default) or until the token expires, whichever
is sooner. Log out clears it. A `dashboard` or `api` token in an
`Authorization` header also reads `/api/live` and the stream, for a
script.

The dashboard's token is separate from the API's: a supervisor holds
what the pages need and not the key to the query API. The cookie
opens the pages and the downloads under `/downloads`, and nothing
under `/api`.

## The label selector

The field in the header narrows a page to sessions whose labels
match, in the familiar syntax: `course=intro-git,term=2026-s2`,
`term!=2025`, `cohort in (a,b)`, `host notin (x)`. Terms are
separated by commas and all must match. The selector is kept in the
URL and carried across the tabs, so a filtered view can be linked to
and the same course is seen on every page.

Labels are what a token or an analytics block attached; the
workshop's name, its collection, the host and the frontend are fields
every session carries. The live view shows them on the row; the
history filters by them too.

## The overview

`/` shows, for a period chosen at the top (the last 7, 30, 90 or 365
days, or all time; 30 days to start with), the journeys started per
day or per week across every workshop, as bars stacked by outcome:
finished, abandoned, lost and still in progress. Days are used for
periods up to ninety days and weeks beyond, and either can be forced.
The figures above the chart are the sums over the period and the
completion rate among the settled journeys, and the note beside them
says how many of the sessions considered were counted, since a
session with missing events is left out of rates unless asked for
([data quality](api.md#completeness-and-data-quality)).

Under the chart, the workshops with sessions in the period: name,
collection, sessions, journeys, finishes, completion rate, last
activity, and a sparkline of the journeys per day over the last
thirty days whatever the period. Each name opens the workshop's page.

Then, for each collection seen, its funnel: the workshops in the
order learners took them, how many learners reached each, finished
it, and stopped there, with how many took them in order and how many
finished every one. A learner here is an instance, one running
JupyterLab, since a collection is worked through in one. A collection
is shown by the title its index gave, with its identity on hover; a
collection whose index declares no `id` is identified by where it was
subscribed from, so two deployments shipping different collections at
the same file path would read as one ([API](api.md#workshop-identity)).

## The live view

`/live` is the sessions being done right now, grouped by workshop,
each row one session showing who is doing it and where, when it
started, the page it is on and its position in the workshop, how many
pages are done, how long since its last event, its status, and its
last few actions and check results. Rows update as batches arrive;
nothing needs reloading. A workshop's heading opens its page.

### What the columns mean

- **Session** is the last eight characters of the session id, linked
  to the session's own page. Beneath it, who and where: the `user`
  when the deployment supplies one (a JupyterHub with `identity:
  hub`) and otherwise a prefix of the instance id, the running
  JupyterLab the session belongs to, then the host and the frontend.
  A learner has one session at a time, so the row is the session; the
  instance prefix is how to tell that a session which just finished
  and the one that followed it are the same person, and sessions of
  one instance sit together within a workshop's group for that
  reason.

- **Elapsed** is how long the session has been running, ticking
  locally between updates, and fixed at its length once it has
  finished or been abandoned. A resumed session counts from the
  resume, not from the session it carried on from, which the hover
  text says.

- **Page** is the current page's file name, its title on hover, and
  its position in the page list the session started with. A session
  whose start was never received has no page list and shows the page
  id alone.

- **Done** counts the pages the learner has left, which is how many
  they have worked through.

- **Last event** is how long ago the session's latest event was
  recorded, ticking locally between updates.

- **Recent** is the last five actions, checks and quizzes, coloured
  by outcome.

### Statuses

Status is derived when the page renders, from the session's
timestamps and the service's thresholds, so a quiet service needs no
timer to age sessions out.

| Status | Meaning |
| ------ | ------- |
| active | An event or a visible heartbeat within `ACTIVE_ALLOWANCE` (150 seconds: more than two missed one-minute beats, so one delayed batch does not flicker the display). |
| away | The latest thing heard was a heartbeat with the tab hidden, within `AWAY_ALLOWANCE` (750 seconds over the five-minute hidden interval). |
| silent | Nothing within the active or away allowance, but less than `SILENT_LIMIT` (30 minutes) of silence. |
| lost | Silence past `SILENT_LIMIT` with no finish or abandon. A closed tab or a culled server looks like this; on Binder it is usually a learner walking away. |
| finished | Finish was pressed on the last page. |
| abandoned | The workshop was closed or replaced before finishing. |
| resumed | A later session named this one in `resumed_from`, so the learner carried on elsewhere: a stopped and restarted codespace, say. |

Finished and abandoned sessions stay on the live view for
`LIVE_LINGER` (five minutes) and then drop off; lost and resumed ones
are not shown there. Every session, whatever its status, is in the
history.

A **gap** marker beside the status means the session is missing
events: every event carries a sequence number, so the service knows
when a batch never arrived. Hovering shows the missing ranges. The
marker appears the moment the event after the gap arrives.

### Under the live view

The page loads `GET /api/live` for its initial state and subscribes
to `GET /api/live/stream`, a Server-Sent Events stream of session
deltas; each delta re-renders that row. The stream sends a keepalive
comment every fifteen seconds, tells a browser that fell behind to
reconnect, and ends with a `closed` event when the service shuts
down, on which the page reconnects until the service is back. The
page also reloads the snapshot every minute so it never drifts from
the server's view. Both take the same `labels` query parameter as the
page.

## The history

`/sessions` lists every session matching the filter bar, newest
first, fifty at a time, with an Older link that carries the cursor
to the next page and a count of how many match in all. The bar
offers the label selector, then the workshop, collection, host and
frontend, each a choice among the values that occur in the data, the
period (all time to start with) or a custom range of dates, and the
statuses, of which several can be ticked at once. The URL carries the
selection with the API's parameter names, `collection=none` standing
for the sessions opened outside any collection, since a form cannot
leave a parameter out the way the API does.

The columns are the live view's without the ticking: the session and
who and where, the workshop with its collection beneath (the name
opens the workshop's page), when it started, when it ended or was
last heard from, its duration, the page it reached with its position,
the pages done, the status with the gap marker, and its labels. Each
session id opens the session's page.

Two links under the table download the same selection, every
session in it and not only the page shown: `sessions.csv`, one row
per session with the fields the API's session summary has, labels as
`key=value` pairs and gaps as `first-last` ranges; and
`events.ndjson`, the stored events of those sessions as JSON lines,
newest session first and each session's events in order, which
`workshop-analytics import` reads back. The downloads are under
`/downloads` so the dashboard cookie covers them.

## A workshop's page

`/workshops/<name>?collection=<collection>` is one workshop's
reports over the period picked at the top, with the same picker and
bucket choice as the overview and the label selector in the header.
A name that has sessions under more than one collection and no
`collection` on the URL is offered the choice, as the API answers
409; links from the other pages always carry the collection. The
Sessions link at the right of the toolbar opens the history narrowed
to the workshop and the period.

- **Needs attention**, when there is something to say: the page more
  journeys stopped on than any other, the check with the lowest pass
  rate and the attempts it took, and how many directives nobody ran.
  Each links to the section beneath that shows it. The card is
  derived from those sections, not a report of its own.

- **The summary**: sessions and journeys, finished (with how many
  finished skipping gates), abandoned, lost, in progress, the
  completion rate, and the medians of the duration of finished
  journeys, of pages done and of directive coverage, with restarts
  and resumes; then a table of the same by version, or by the
  dimension chosen in the toolbar's Compare by: host, frontend,
  version, collection, platform, or any label key seen.

- **The trend**: journeys started per day or per week, stacked by
  outcome. With Compare by set, one bar per value of the dimension
  side by side in each period, which is how Binder is read against
  Codespaces or one release against the next.

- **The funnel**: each page in the workshop's order with how many
  journeys reached it, left it, and stopped on it, the reach as a bar
  and the stops as its red tail.

- **Time on each page**: sessions, entries per session, and the
  median, ninetieth percentile and longest active time, from the
  page leaves recorded.

- **What nobody ran**: per page, the directives no session with an
  inventory ran, each with its type and how it was expected to start
  (`click`, `auto`, `cascade` or `trigger`) and whether it was
  conditional and so may never have been shown. Only sessions from
  extension 0.2.1 and later carry an inventory; the heading says how
  many did.

- **Checks and quizzes**: per check, the sessions that ran it and
  passed, the pass rate, the median and most attempts before passing,
  errors and skips, and what set it off; then the hints opened and
  the gates skipped.

The reports are the API's `workshops/{name}` summary, `trends`,
`funnel`, `pages`, `coverage` and `checks` routes ([API](api.md))
with the same filters, so the numbers here are the numbers a script
or the [MCP server](mcp.md) gets. A workshop's sessions are loaded
into memory for each report, bounded by the period, which is fine at
the scale of a few thousand sessions.

## A session's page

Clicking a session id opens `/sessions/<id>`, the session on its own:
a summary card with its status, when it started and ended or was
last heard from, its duration, its progress through the pages with
any gates it skipped, how many of its events arrived, its workshop,
labels and token, and the chain of sessions it belongs to when it
resumed another or was resumed; a table of the workshop's pages with
whether each was entered and left, the active time on it, how many
times it was entered and, for a session from extension 0.2.1 or
later, how many of the page's directives the session ran out of
those listed, the ones never run named on hover (unknown for an
older session, which sent no inventory); and the timeline, every
event in order with its kind, page, id, status and the kind's own
fields, with a marked row wherever events are missing. The page is
rendered on the server from the same query the API answers at
`/api/sessions/{id}`, and the workshop's name in its heading opens
the workshop's page.
