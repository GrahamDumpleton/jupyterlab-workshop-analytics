# The command line

`workshop-analytics` is the one console script, the same in a checkout
and in the container image, so every command works through
`kubectl exec` in a deployed pod with the key and the database already
in its environment. Every command reads its settings from the
environment; see [development](development.md#settings) for the full
list.

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
predates labels. The command prints how many events were received,
stored, already present and rejected, with the first few reasons.

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
