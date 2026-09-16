---
name: jupyterlab-workshop-analytics
description: Report on how learners progress through jupyterlab-workshop workshops using the analytics service's MCP tools or REST API. Use when asked how many did a workshop, where they stop, how long pages take, whether checks pass, which learners have not finished, or what one session did.
---

# Reporting on workshop progress

The analytics service holds the progress events that jupyterlab-workshop
workshops report and answers questions about them. Every definition
comes from the service, not from this file: read `describe` first and
trust it over anything remembered.

## Connecting

Two doors, one token with the `api` scope, sent as
`Authorization: Bearer <token>`:

- **MCP**, streamable HTTP at `<base>/mcp`, one tool per question.
  Add it to Claude Code with
  `claude mcp add --transport http workshop-analytics <base>/mcp --header "Authorization: Bearer <token>"`.

- **REST**, `GET <base>/api/...`, the same questions as routes, with
  the OpenAPI document at `<base>/docs` and the meaning on the
  service's `docs/api.md`.

The tools and routes take the same filters, so a recipe below works
through either.

## Start every report the same way

1. `describe`. It states the event kinds, the session statuses with
   the thresholds this deployment runs with, how each metric is
   defined, the filters, and what the store holds: label keys and
   their values, collections, hosts, frontends, whether sessions
   carry a learner identity (per token and per host), and whether
   the SQL tool is on, with the table definitions and the dialect.

2. `list_workshops`. Workshops are identified by `name` and
   `collection`; the same name can exist in two collections. When a
   name-keyed tool says the name is ambiguous, pass `collection` from
   the list it gives. An empty collection means sessions opened
   outside any collection.

3. Read the `data_quality` note on every summary before quoting a
   number. `considered` is what the filters matched, `included` what
   the answer used; the difference is sessions with missing events.
   A completion rate over three included sessions of thirty
   considered is not a completion rate.

## The standard reports

- **How many did it, and how did it go.** `workshop_summary` for the
  workshop: sessions, journeys, starts, resumes, finished, abandoned,
  lost, in progress, completion rate, duration percentiles, in total
  and by version (or `group_by` another field or a label key). Quote
  `finished_skipping_gates` beside the completion rate: those finishes
  moved past unmet requirements under soft gating, so the workshop's
  own checks did not confirm them.

- **Where they stop.** `funnel`: journeys entering, leaving and
  stopping on each page in order. The page most journeys stop on is
  the one to look at; `page_timing` says how long pages take and
  `action_usage` and `checks` say what happened on them.

- **Where the time goes.** `page_timing`: active seconds per page as
  percentiles. Time is wall clock while the page is shown, so a tab
  left open inflates it; prefer `p50` over `mean`.

- **Whether the clickable actions are used.** `action_usage`:
  `clicked` and `sessions_clicking` against `sessions`, then per
  action the runs by trigger and the errors and skips, with `listed`
  saying how many sessions had the action to run.

- **What nobody clicked.** `coverage`: per page and directive, the
  sessions whose page list named it (`listed`) and the sessions that
  ran it (`ran`), and `never_run` for the directives no session ran.
  Read each with its `trigger`: a `click` nobody pressed is a button
  learners skip; an `auto` that never fired or a `cascade` whose
  predecessor never succeeded is the workshop not working as written;
  a `trigger` nothing set off is a check the learner never reached.
  A `conditional` directive may never have been shown, so do not
  report it as skipped. Only sessions from extension 0.2.1 and later
  carry the inventory: `with_inventory` says how many did, and a
  workshop with none has no coverage to report, not zero.

- **Whether the checks pass.** `checks`: per verify or quiz the
  sessions that ran it, the sessions that passed, the pass rate and
  the attempts before the first pass, with hints opened and gates
  skipped beside them.

- **How it changes over time.** `trends` with `bucket=day` or
  `week`, and `group_by` to compare cohorts across the periods.

- **Across a collection.** `list_collections` for the workshops in
  the order learners took them, `collection_progress` for how many
  instances took each, took them in order, and where they stopped.

- **Who has not finished.** Only where `describe` says sessions carry
  a `user`: `learners` groups a workshop's sessions by learner with
  attempts, best progress and completion. Without identity the
  service can say how many, never who; an instance groups the
  sessions of one running JupyterLab, which is the closest thing.

- **Right now.** `live`: the sessions in progress as the dashboard
  shows them, with status, page and time since the last event.

## One session

`list_sessions` finds sessions by any filter, newest first;
`session_timeline` gives one session's summary, its pages with time
spent, every event in order, its gaps and the chain of sessions it
belongs to; `session_events` gives the raw events. For a supervised
class, read the timeline from the top: the start with its page list,
then page enters and leaves, actions and their outcomes, check
results with their attempt numbers, heartbeats saying whether the
tab was visible, and the finish or abandon, or silence.

## Slicing

- **Labels** are what a deployment or an author chose to say:
  `course=intro-git,term!=2025,cohort in (a,b)`. `describe` lists the
  keys and values seen and marks the ones a token binds, which are
  the operator's word.

- **Fields** are facts the event carried: `host`, `frontend`,
  `platform`, `version`, `source`, `collection`, `token_id`, `user`.
  Use a field when the question is about where or how the workshop
  ran, a label when it is about who it was run for.

- `since` and `until` are ISO 8601 UTC and select by when a session
  started; `status` selects by the derived status.

- `include_incomplete=true` counts sessions with missing events in
  rates and timings. Acceptable for a count of starts; not for a
  completion rate or a timing.

## The SQL tool

When `describe` says it is enabled, `sql` runs one SELECT or WITH
against the store in its dialect, rows capped and stopped at a
deadline. Use it for what no tool answers: joins across sessions and
events, sequences of pages or attempts, cohorts by weekday or hub
user, late-arriving batches under a token. `describe` gives the
tables, the columns and the notes on the JSON columns; the extracted
columns on `events` answer most questions without JSON functions.
Aggregate in SQL rather than fetching events to count them.

## What the numbers cannot say

- The session is the unit. A chain of sessions linked by resumes is
  one journey, and rates count journeys.

- `lost` means the heartbeats stopped without a finish or abandon: a
  closed tab, a culled server, or a learner who walked away. On
  Binder it is usually the last.

- A `gap` means a batch never arrived; the service knows from the
  sequence numbers. A resend closes it. Such sessions are left out of
  rates by default.

- Page time is wall clock with the page shown, not attention.

- Events never carry file contents, command output, form answers or
  variable values, so nothing here says what a learner wrote.
