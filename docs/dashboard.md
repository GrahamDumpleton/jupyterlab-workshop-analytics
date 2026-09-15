# The dashboard

`GET /` is one page: the sessions being done right now, grouped by
workshop, each row showing who is doing it, when they started, the
page they are on and its position in the workshop, how many pages they
have finished, how long since their last event, their status, and
their last few actions and check results. Rows update as batches
arrive; nothing needs reloading.

## Signing in

The page is entered with a token carrying the `dashboard` scope
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
what the page needs and not the key to the query API.

## The label selector

The field in the header narrows the page to sessions whose labels
match, in the familiar syntax: `course=intro-git,term=2026-s2`,
`term!=2025`, `cohort in (a,b)`, `host notin (x)`. Terms are
separated by commas and all must match. The selector is kept in the
URL, so a filtered view can be linked to.

Labels are what a token or an analytics block attached; the
workshop's name, its collection, the host and the frontend are fields
every session carries and are shown on the row rather than filtered
here.

## What the columns mean

- **Learner** is the `user` when the deployment supplies one (a
  JupyterHub with `identity: hub`) and the last eight characters of
  the session id otherwise. Beneath it, the host, the frontend and a
  prefix of the instance id: the running JupyterLab the session
  belongs to. Sessions of one instance sit together within a
  workshop's group, so a learner doing the second workshop of three
  reads as one line of progress.

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

## Statuses

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

Finished and abandoned sessions stay on the page for `LIVE_LINGER`
(five minutes) and then drop off; lost and resumed ones are not
shown.

A **gap** marker beside the status means the session is missing
events: every event carries a sequence number, so the service knows
when a batch never arrived. Hovering shows the missing ranges. The
marker appears the moment the event after the gap arrives.

## Under the page

The page loads `GET /api/live` for its initial state and subscribes
to `GET /api/live/stream`, a Server-Sent Events stream of session
deltas; each delta re-renders that row. The stream sends a keepalive
comment every fifteen seconds, tells a browser that fell behind to
reconnect, and ends with a `closed` event when the service shuts
down, on which the page reconnects until the service is back. The
page also reloads the snapshot every minute so it never drifts from
the server's view. Both take the same `labels` query parameter as the
page.
