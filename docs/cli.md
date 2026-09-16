# The command line

`workshop-analytics` is the one console script, the same in a checkout
and in the container image, so every command works through
`kubectl exec` in a deployed pod with the key and the database already
in its environment. Every command reads its settings from the
environment; see [development](development.md#settings) for the full
list.

## In a deployment

The container's command is `serve`, and the other commands run in the
same container against the same store and key:

```console
$ kubectl -n workshop-analytics exec deploy/workshop-analytics -- \
    workshop-analytics token issue --name hub --expires 2027-01-31 --label deployment=hub
$ kubectl -n workshop-analytics exec deploy/workshop-analytics -- \
    workshop-analytics rebuild
$ kubectl -n workshop-analytics cp events.jsonl workshop-analytics-<pod>:/tmp/events.jsonl
$ kubectl -n workshop-analytics exec deploy/workshop-analytics -- \
    workshop-analytics import /tmp/events.jsonl --label course=intro
$ kubectl -n workshop-analytics exec deploy/workshop-analytics -- \
    workshop-analytics export > backup.jsonl
```

With Docker it is `docker exec <container> workshop-analytics ...`.
`key generate` is the one command to run on a laptop instead, since
its output goes into the Secret the pod is started with, and `token
inspect` runs anywhere, since it needs no key. `serve` migrates the
store at start, so `migrate` is only for a deployment that wants
migrations as a separate step, a Job before the rollout say. The
image, the manifests and the per-host settings are on
[deploying](deploying.md).

## serve

```console
$ workshop-analytics serve [--host 127.0.0.1] [--port 8080] [--no-migrate]
```

Runs the service on uvicorn. The store is brought up to the latest
migration first unless `--no-migrate` is given, so a fresh volume is
usable at once. `TOKEN_SIGNING_KEY` must be set; the command refuses
to start without it.

Ctrl-C, or SIGTERM in a container, stops it cleanly: every open
dashboard stream is sent a closing frame and ends, the server drains,
and anything still open after five seconds is cut. Without that step
uvicorn would wait for the streams, which never end on their own.

When `WRAPTURE_CONFIG` names a file, the command applies that wrapture
configuration before importing the application, which is what
`just serve` does with the repository's `wrapture.toml`. Without the
variable the service runs untraced. See
[observability](observability.md).

## migrate

```console
$ workshop-analytics migrate
```

Brings the database at `DATABASE_URL` up to the latest schema. `serve`
does this itself; the command exists for a deployment that wants
migrations as a separate step.

## rebuild

```console
$ workshop-analytics rebuild
```

Drops and recomputes every row of the sessions projection from the
stored events. The projection is derived, so this is always safe; run
it after upgrading to a release that changed what a session row
holds.

## import

```console
$ workshop-analytics import path/to/events.jsonl [--token <token>] [--label key=value ...]
```

Feeds a file through the same pipeline as the sink, for an offline
class that exported its events with "Workshop: Export Progress
Events", and for seeding a local store from the test fixtures. The
file is JSON lines, one event per line, as the extension writes it; a
`.json` file is read as a JSON array. Each line carries its own
labels, so no arguments are needed.

`--token` names a signed ingest token whose `jti` and labels the
import counts under, exactly as if the file had been posted with it;
`--label` adds a trusted label for this import alone, for a file that
predates labels. A file written by `export` is recognised by its shape
and restores what it recorded, described below. The command prints
how many events were received, stored, already present and rejected,
with the first few reasons.

## export

```console
$ workshop-analytics export [--output events.jsonl] [--selector key=value,...] \
    [--name <workshop>] [--collection <url>] [--since <when>] [--until <when>]
```

Writes the stored events as JSON lines, oldest first, to the file or
to standard output, and says on standard error how many. Each line
holds the event as it was received under `event`, beside what the
store added: the labels it was kept with, the `token_id` it arrived
under and `received_at`. Those are what a plain events file does not
carry, and `import` restores all of them from this format, the labels
trusted as stored rather than merged as a sender's, since whoever
runs the import owns the store. The events table is the whole store,
sessions being derived from it and projected as the import goes, so a
deployment moves to a fresh instance, or from SQLite to PostgreSQL,
by exporting from one and importing into the other.

The filters are the API's: a label selector, a workshop by name, a
collection (an empty string for sessions outside any), and `--since`
and `--until` on the event's own timestamp. Events are deduplicated
by content, so a file loaded twice stores nothing the second time,
and a periodic export is a backup that can always be applied.

## key generate

```console
$ workshop-analytics key generate
```

Prints a fresh random signing key, base64, for `TOKEN_SIGNING_KEY`.
Needs no key itself and runs anywhere.

## token issue

```console
$ workshop-analytics token issue --name <who> --expires <when> \
    [--label key=value ...] [--origin <url> ...] [--scope ingest|api|dashboard ...] \
    [--key-file <path>] [--json]
```

Signs a token and prints it with its `jti` and expiry. The details
are on the [tokens](tokens.md) page.

## token inspect

```console
$ workshop-analytics token inspect <token>
```

Prints a token's claims as JSON without verifying it, so no key is
needed. `-` reads the token from standard input.

## Exit status

Every command exits 0 when it did what was asked and 2 with a one-line
message on standard error when it could not: no key, an unreadable
file, a bad option value. `import` exits 1 when every line was
rejected.
