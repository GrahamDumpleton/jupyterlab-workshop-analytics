# The MCP server

The service exposes its questions as tools over the Model Context
Protocol at `/mcp`, so an assistant can ask them directly. The tools
call the same functions the [query API](api.md) routes call, so the
two never answer differently; the API page carries the meaning of the
filters, the statuses, the metrics and the data quality note, and
this page carries how to connect and what the tools are.

## Connecting

The transport is streamable HTTP, stateless, with a token carrying the
`api` scope ([tokens](tokens.md)) as `Authorization: Bearer <token>`.
There is no OAuth flow: the service's own tokens are the credential,
verified by the same code that guards `/api`, with the same deny list
and the same refusal log.

With Claude Code:

```console
$ claude mcp add --transport http workshop-analytics \
    http://127.0.0.1:8080/mcp \
    --header "Authorization: Bearer <token>"
```

Any client that speaks streamable HTTP connects the same way. A
request without a token, or with one that lacks the scope, answers
401 before the protocol is spoken.

## The tools

| Tool | Answers |
| --- | --- |
| `describe` | What the store holds and how every answer is computed. Read first. |
| `list_workshops` | Every workshop seen, as name and collection pairs. |
| `list_collections` | The collections seen with their workshops in the order taken. |
| `collection_progress` | The funnel across a collection's workshops, by instance. |
| `workshop_summary` | Outcomes in total and by a dimension. |
| `funnel` | Journeys reaching, leaving and stopping on each page. |
| `page_timing` | Time on each page. |
| `action_usage` | Runs by trigger and outcome per action. |
| `coverage` | What nobody ran, per page and directive. |
| `checks` | Verify and quiz pass rates, attempts, hints, gates. |
| `trends` | Outcomes by day or week, of one workshop or across every session the selection matches. |
| `list_sessions` | Sessions newest first, cursor paged. |
| `session_timeline` | One session with its pages, timeline, gaps and chain. |
| `session_events` | One session's raw events. |
| `query_events` | Raw events by any filter, cursor paged. |
| `instance` | One running frontend's sessions in order. |
| `learners` | Sessions by learner identity. |
| `live` | The sessions in progress right now. |
| `sql` | One read-only SQL statement, when the tool is enabled. |

The name-keyed tools take `name` and the rest of the common filters
as one `select` object with the fields the API takes as query
parameters: `labels`, `collection`, `source`, `version`, `frontend`,
`host`, `platform`, `token_id`, `user`, `since`, `until`, `status`
and `include_incomplete`. `workshop_summary` and `trends` add
`group_by`, `trends` adds `bucket` and takes `name` as optional, the
paged tools add `limit` and `cursor`, and `query_events` adds the
event's own fields.

A refusal is a tool error with the reason as its text: an ambiguous
name says which collections it has, so the next call adds
`collection`; a bad selector, timestamp, status, cursor or bucket
says what was wrong; a statement the SQL tool will not run says why.

## The skill

`skills/jupyterlab-workshop-analytics/SKILL.md` in this repository is
a skill for an assistant reporting on workshops: how to connect, to
start with `describe` and `list_workshops`, the recipes for the
standard reports, how to read one session's timeline, how to slice by
labels and fields, how identity changes the questions, how to read
the data quality note, and what the numbers cannot say. It stays short
because the definitions come from the service. Install it wherever
the assistant reads skills from, or point the assistant at the file.

## Under the transport

The server is the official Python SDK's `MCPServer`, mounted into the
same ASGI application as the API behind a bearer guard, its session
manager run by the application's lifespan. Responses are plain JSON
rather than event streams, and no session is kept between requests,
so the endpoint works behind any ingress and across replicas. DNS
rebinding protection is off, since the service runs under a real host
name and the token is the guard.
