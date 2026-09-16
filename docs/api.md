# The query API

Every question the service answers is a `GET` under `/api`, taking a
token with the `api` scope as `Authorization: Bearer <token>`
([tokens](tokens.md)) and answering JSON. The OpenAPI document at
`/docs` is the exact reference for every route, parameter and response
field; this page carries the meaning: what the filters select, how a
workshop is identified, what the statuses and the completeness columns
say, how the metrics are defined, and what the data quality note on
every answer means.

Start with `GET /api/describe`. It returns the event contract, the
session rules with the thresholds this deployment runs with, the
metric definitions, the filters, and what the store actually holds:
the label keys and values seen, the collections, the hosts and
frontends, and whether sessions carry a learner identity, per token
and per host. A client that reads it first asks questions of fields
that exist with values that occur. The same questions are tools on
the [MCP server](mcp.md) at `/mcp`.

## The routes

| Route | Answers |
| --- | --- |
| `GET /api/describe` | Event kinds and fields, session statuses and completeness rules, metric definitions, filters, label keys with token-bound ones marked, collections seen, identity availability, the schema version and the dialect. |
| `GET /api/workshops` | Discovery: every `name` and `collection` pair with sessions, with the sources and versions seen, session and journey counts, finishes, completion rate, share complete, first and last activity, and whether any session carried an identity. |
| `GET /api/collections` | The collections seen, each with its workshops in the order instances took them, with session counts and completion. |
| `GET /api/collections/progress?collection=` | The collection-level funnel: how many instances took each workshop, how many took them in order, and where they stopped. |
| `GET /api/workshops/{name}` | Outcomes in total and by version (or another `group_by`): sessions, journeys, starts, restarts, resumes, finished, finished skipping gates, abandoned, lost, in progress, completion rate, duration and pages-done percentiles. |
| `GET /api/workshops/{name}/funnel` | Journeys reaching each page in order, leaving it, and stopping on it. |
| `GET /api/workshops/{name}/pages` | Time on each page from `page-leave`, as percentiles, with entries per session. |
| `GET /api/workshops/{name}/actions` | Per action id: runs by trigger, ok, error, skipped and downgraded, and whether the clickable actions are used at all. |
| `GET /api/workshops/{name}/coverage` | What nobody ran: per page and directive, the sessions whose page list named it and the sessions that ran it, with how each was expected to start. |
| `GET /api/workshops/{name}/checks` | Verify and quiz pass rates, attempts before passing, hints opened and gates skipped. |
| `GET /api/workshops/{name}/trends` | The outcomes bucketed by day or week, and by a `group_by` dimension when asked. |
| `GET /api/trends` | The same buckets across every session the filters match, or of one workshop when `name` is given; `workshop` is null for the total. |
| `GET /api/workshops/{name}/sessions` | The workshop's sessions, newest first, cursor paged. |
| `GET /api/workshops/{name}/learners` | Sessions grouped by learner identity, with attempts, best progress and completion, and how many sessions carried no identity. |
| `GET /api/sessions` | Sessions across every workshop by any filter, newest first, cursor paged. |
| `GET /api/sessions/{id}` | One session: its summary, its pages with time spent, its ordered timeline, its gaps and the chain it belongs to. |
| `GET /api/sessions/{id}/events` | The raw events of one session, JSON or JSON lines. |
| `GET /api/events` | The escape hatch: raw events by any filter, cursor paged, JSON or JSON lines. |
| `GET /api/instances/{id}` | The sessions of one running frontend in the order they started, with each workshop's outcome. |
| `POST /api/sql` | One read-only SQL statement against the store, when the tool is enabled. See below. |
| `GET /api/live`, `GET /api/live/stream` | The sessions in progress and the event stream ([dashboard](dashboard.md)); these two also accept a `dashboard` token or the dashboard's cookie. |
| `GET /healthz` | Liveness; no token. |

## The common filters

Every route that selects sessions takes the same query parameters,
all optional, all combined with "and":

| Parameter | Selects |
| --- | --- |
| `labels` | A label selector: `course=intro-git,term!=2025`, `cohort in (a,b)`, `host notin (x)`. Terms are separated by commas and every term must match. |
| `name` | The workshop's manifest name. On the name-keyed routes the name comes from the path instead. |
| `collection` | The collection the workshop was subscribed from. Empty (`collection=`) selects sessions opened outside any collection. |
| `source`, `version`, `frontend`, `host`, `platform` | The session's fields, as the events carried them. |
| `token_id` | The `jti` of the token the session's first batch arrived under. |
| `user` | The learner's identity, where the deployment supplies one. |
| `since`, `until` | ISO 8601 UTC, a date alone accepted. Sessions started at or after `since` and before `until`; on `/api/events`, the event's own timestamp. |
| `status` | One or more session statuses, comma separated. Status is derived at read time, so this is applied after the rows are read. |
| `include_incomplete` | `true` to count sessions with missing events in rates and timings. See data quality below. |

The summary and trends routes add `group_by`, which is one of
`collection`, `version`, `frontend`, `host`, `platform`, `user`, or
any label key; a journey is grouped by its first session's value.
The trends routes add `bucket`, `day` or `week`. The paged routes add
`limit` (at most 1000) and `cursor`, the `next_cursor` of the previous
page; an empty `next_cursor` is the last page. The event routes add
`format`, `json` or `ndjson`, and honour an `Accept:
application/x-ndjson` header the same way.

A malformed selector, timestamp, status or cursor answers 400 with the
reason in `detail`.

## Workshop identity

A workshop is identified in the data by the pair of its manifest
`name` and its `collection`, since two collections can each list an
`intro-git`. `source` is not part of the identity: it names where a
copy came from and differs between a local checkout and a git URL for
the same workshop.

The name-keyed routes keep the name in the path because it is unique
in nearly every deployment. A request without `collection` succeeds
when exactly one collection has sessions under that name, or when only
sessions opened outside any collection do. When more than one does,
the route answers 409 with the candidates:

```json
{
  "detail": {
    "error": "ambiguous",
    "message": "the workshop \"intro-git\" has sessions under more than one collection; add collection= to choose one",
    "name": "intro-git",
    "collections": ["", "https://example.org/collection.json"]
  }
}
```

The next request adds `collection=` with one of them, and every answer
echoes the pair it was answered for as `workshop`.

## Sessions, journeys and outcomes

A **session** is one open of a workshop. Its status is derived when
the question is asked, from its timestamps and the deployment's
thresholds, which `describe` reports:

| Status | Meaning |
| --- | --- |
| `active` | An event or a visible heartbeat within `ACTIVE_ALLOWANCE` (150 seconds by default). |
| `away` | The latest thing heard was a heartbeat with the tab hidden, within `AWAY_ALLOWANCE` (750 seconds). |
| `silent` | Nothing within the active or away allowance, but less than `SILENT_LIMIT` (30 minutes) of silence. |
| `lost` | Silence past `SILENT_LIMIT` with no finish or abandon: a closed tab, a culled server, or a learner who walked away. |
| `finished` | Finish was pressed on the last page. |
| `abandoned` | The workshop was closed or replaced before finishing. |
| `resumed` | A later session named this one in `resumed_from`; the learner carried on there. |

A **journey** is a chain of sessions linked by `resumed_from`: a
learner who stopped a codespace overnight and continued the next day
is one journey of two sessions. Counts of outcomes are per journey,
and a journey's outcome is its last session's: `finished`,
`abandoned`, `lost`, or `in_progress` for a session still active,
away or silent. A session that resumes one outside the filters starts
a chain of its own, so a filter that cuts a chain still counts what it
matched.

The metrics, as `describe` also states them:

- **Completion rate** is finished journeys over settled journeys,
  where settled is finished plus abandoned plus lost. Journeys still
  in progress are left out of both sides.

- **Finished skipping gates** is, of the finished journeys, those in
  which a session moved past unmet requirements under soft gating at
  least once. The completion rate counts them as finished, since
  Finish was pressed, so a report quoting the rate says how many of
  its finishes the workshop's checks did not confirm.

- **Duration** is, for a finished journey, the time from each
  session's start to its end or last event, summed over the chain, so
  the time between a stop and a resume is not counted. Percentiles are
  over the finished journeys.

- **Pages done** is the pages a session left, which is how many it
  worked through; a journey's is the distinct pages left across its
  sessions.

- **Funnel** counts, per page in the order the sessions ran with, the
  journeys that entered it, the journeys that left it, and the
  journeys not finished whose furthest page in the list is that one.
  The page order is the page list most sessions started with, with any
  page other sessions listed appended; a page entered that no list
  names is reported under `unlisted_pages`.

- **Page timing** comes from `page-leave`'s `active_ms`, one value per
  leave, so a page entered twice contributes twice. The last page is
  rarely left, so it usually has no timing.

- **Actions** count runs by trigger (`click`, `role`, `auto`,
  `cascade`, `trigger`) and by outcome, and `clicked` with
  `sessions_clicking` say whether learners use the clickable actions
  at all. `listed` is how many sessions had the action to run, from
  the inventory below, so runs can be read against opportunities.

- **Coverage** is what nobody ran. From extension 0.2.1 the page list
  a session starts with names the directives on each page and how
  each is expected to start (`click`, `auto`, `cascade` or `trigger`,
  the words the events use), with `conditional` marking one a `when`
  block or option may hide. The report counts, per page and directive,
  the sessions whose inventory named it and the sessions whose events
  report it ran, matched by id, and lists under `never_run` the
  directives no session ran. A `click` nobody pressed, an `auto` that
  never fired, a `cascade` whose predecessor never succeeded and a
  `trigger` nothing set off are different findings, and a conditional
  directive may never have been shown. A session's `coverage` is the
  share of its listed directives it ran; the outcomes carry the
  percentiles over journeys, the coverage report over sessions, and a
  session without an inventory is left out rather than read as zero.
  `with_inventory` in the data quality note says how many took part.

- **Checks** count, per verify or quiz id, the sessions that ran it,
  the sessions that passed it at least once, the pass rate as the
  latter over the former, and the attempt number of each session's
  first pass as percentiles. A quiz passes when answered correctly.
  Hints are reported by id beside them, and the gates skipped under
  soft gating are counted.

- **Trends** bucket journeys by the day or ISO week their first
  session started in, in UTC, and list every bucket between the first
  and the last so a chart has no holes.

- **Collections** order a collection's workshops by the average
  position each had among the workshops every instance opened, so the
  order most learners followed comes out even when nobody took every
  workshop. `in_order` counts instances whose workshops were opened in
  non-decreasing collection order; `stopped` is the last workshop an
  instance opened unless it finished them all.

Percentiles are nearest rank over the values, reported as `count`,
`min`, `p50`, `p90`, `max` and `mean`.

## Completeness and data quality

Every event carries a sequence number, so the service knows when a
batch never arrived. Each session reports `events_received`,
`events_expected` (the `seq` of the terminal event when a finish or
abandon arrived, otherwise the highest `seq` seen), `gaps` as
`[first, last]` ranges below that, and `complete`, true only when a
terminal event arrived and nothing is missing.

Three kinds of loss are told apart because they mean different
things. A gap in the middle is a delivery failure, and a resend closes
it since events are deduplicated by content. A missing tail, a session
with no terminal event that went silent, is open-ended, and on Binder
is usually a learner walking away. A missing head, the first events
never arriving, also means no page list to resolve page ids against.

Rates and timings leave out the first and the third kind by default,
since what they say cannot be trusted, and count the second, since
silence is an outcome on its own. Every summary carries a
`data_quality` object saying what happened:

| Field | Meaning |
| --- | --- |
| `considered` | Sessions the filters matched. |
| `included` | Sessions the answer used. |
| `excluded_gaps` | Left out for a missing range after the first event. |
| `excluded_missing_head` | Left out because the first events never arrived. |
| `open_ended` | Included sessions that stopped without a finish or abandon (status `lost`). |
| `in_progress` | Included sessions still active, away or silent. |
| `complete` | Sessions whose terminal event arrived with nothing missing. |
| `share_complete` | `complete` over `considered`. |
| `include_incomplete` | Whether the caller asked for the excluded sessions to be counted. |

`include_incomplete=true` widens the net for a question that tolerates
it: a count of starts does, a completion rate does not. The session
listings, the session detail, the learners and the event queries
always return everything, with each session's `complete`, `gaps` and
`events_expected` beside it, so a reader can look at the loss
directly. Read the quality note before drawing a conclusion: a
completion rate over three included sessions of thirty considered is
not a completion rate.

## Sessions and events

`GET /api/workshops/{name}/sessions` and `GET /api/sessions` list
sessions newest first with `total`, the count the filters matched,
and `next_cursor`. Each session carries its identity fields, its
labels as the service merged them (a token's labels win, a colliding
client label is kept under the `client.` prefix), its status, its
start, last event, end and duration, its current page with position
and count, pages done, the completeness columns, and its links:
`resumed_from`, `resumed_by`, `restarted_from`.

`GET /api/sessions/{id}` adds `pages`, each page of the list the
session started with (plus any entered outside it) with whether it
was entered and left, the active seconds on it and the entries;
`timeline`, every stored event reduced to `seq`, `ts`, `kind`,
`page`, `id`, `status` and a `detail` object of the kind's own
fields; `gaps`; and `chain`, the session ids of its journey, oldest
first.

`GET /api/sessions/{id}/events` and `GET /api/events` return the
events as they were stored: the event as it arrived, with `labels`
replaced by the merged labels and `received_at` and `token_id` added.
`/api/events` takes the common filters (`since` and `until` on the
event's own timestamp, `status` on the session) plus `kind`,
`session_id`, `instance_id`, `page`, `id` (the action, check, quiz,
form or hint id) and `event_status` (the event's own status field),
oldest first, cursor paged. The label selector is applied after the
page is read, so a page can come back short of `limit` before the
cursor ends.

Timestamps everywhere are ISO 8601 UTC with a `Z`.

## Instances

An instance is one running frontend: one run of the Jupyter server
for JupyterLab, one page load for JupyterLite. Every workshop opened
under that run carries the same `instance_id`, which is how the
sessions of one learner working through a collection are grouped
without an identity. `GET /api/instances/{id}` lists them in the order
they started, and the collection routes count instances rather than
sessions.

## Identity

`user` is present on a session when the deployment's identity policy
adds one, such as the JupyterHub user name under `identity: hub`, and
absent otherwise. `describe` reports the share of sessions carrying
one, per token and per host, so a client knows which questions it can
answer: a hub can be asked who has not finished, an anonymous
deployment only how many. `GET /api/workshops/{name}/learners` groups
by `user` and counts the sessions that carried none as
`anonymous_sessions`.

## The SQL tool

`POST /api/sql` runs one statement against the store in its own
dialect, for the questions nobody wrote a route for: joins across
sessions and events, sequences of pages or attempts, cohorts by
weekday or hub user, late-arriving batches under a token. The body is
JSON:

```json
{"sql": "select name, count(*) as n from sessions where user = :u group by name",
 "params": {"u": "alice"},
 "limit": 100}
```

and the answer is the columns, the rows, the row count, whether the
rows were cut at the cap, and the time taken:

```json
{"columns": ["name", "n"], "rows": [["intro-git", 3]], "row_count": 1,
 "truncated": false, "elapsed_ms": 0.4}
```

What keeps it safe is what it cannot do. The statement must be a
single `SELECT`, `WITH` or `EXPLAIN`; a semicolon may only end it,
and anything else answers 400 with the reason. It runs on a second
engine opened read-only (`mode=ro` and `query_only` on SQLite, a
read-only transaction on PostgreSQL, whose role should be read-only
too), so a `WITH` that writes fails there. It is stopped at
`SQL_TIMEOUT` seconds (10 by default), answering 408, by a progress
handler on SQLite and `statement_timeout` on PostgreSQL. Its rows are
capped at `SQL_MAX_ROWS` (1000), and `limit` in the body lowers the
cap for one call. A deployment turns the tool off with `SQL_TOOL=off`,
after which the route answers 404 and `describe` says so.

`describe` carries a `sql` object with the tool's state, the dialect,
the limits, the two tables with their columns, primary keys and
indexes, and notes on writing against them: `labels` and `payload`
are JSON columns, read with `json_extract(payload, '$.field')` on
SQLite and `payload ->> 'field'` on PostgreSQL; timestamps are naive
UTC, and on SQLite come back as the stored text; the extracted columns
on `events` answer most questions without JSON functions; session status is not stored, so derive it from the
timestamps or ask the curated routes, which apply the thresholds.

The data holds no secrets, since events never carry file contents,
command output, form answers or variable values; the tool is on by
default for that reason.

## Labels

`describe` lists every label key seen with its values, the number of
sessions carrying it, and the tokens under which it is bound. The
store keeps the merged labels rather than which came from the token,
so a key is reported as bound when every session that arrived under a
token carries it with a single value for that token, which is what a
token's label looks like and a client's rarely does.
