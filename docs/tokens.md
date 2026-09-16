# Tokens

The service holds one credential, the signing key, and every token it
ever issues is a JWT signed with it. There is no token file and no
token table: everything the service needs to know about the holder is
in the token's claims, so issuing one is signing, revoking one is
listing its id, and rotating the key invalidates them all at once.

## The signing key

`TOKEN_SIGNING_KEY` is a base64 string of at least 16 random bytes.
Make one with the command, which prints 32:

```console
$ workshop-analytics key generate
```

The service refuses to start without it, plainly, rather than on the
first request. In a cluster it lives in a Secret and nowhere else; a
token is never stored server-side, so the key is the only thing to
protect. Changing it is the break-glass move: every token signed with
the old key stops verifying at once.

## Claims

| Claim | Meaning |
| ----- | ------- |
| `jti` | The token's id, a UUID minted at issue. Rows keep it as `token_id`, and it is what the deny list names. |
| `sub` | Who the token was issued to: a deployment, a collection author, a supervisor. Free text, for the operator's records. |
| `scope` | What it may do: `ingest` (post events), `api` (read the [query API](api.md) and the [MCP server](mcp.md)), `dashboard` (open the live view). A token may carry several. |
| `labels` | Labels bound to every event posted under the token, trusted because they are signed. See below. |
| `origins` | Browser origins the token may post from, for a JupyterLite site. Empty for a server-side sender. |
| `nbf`, `exp` | The validity window. `exp` is required at issue; there are no non-expiring tokens. |

## Issuing

```console
$ workshop-analytics token issue --name showcase-binder \
    --label deployment=showcase-binder --expires 2027-01-31
token: eyJhbGciOi...
jti: 4c0f...
expires: 2027-01-31T23:59:59Z
```

`--name` and `--expires` are required. The expiry is a date (the end
of that day in UTC), a timestamp, or a duration from now such as
`90d`, `12h` or `30m`; issue tokens for a course or a season, not
forever, since expiry is the everyday control. `--label` may be
repeated and takes `key=value`; `--origin` may be repeated; `--scope`
may be repeated and defaults to `ingest`; `--json` prints the token
and its claims together for a script. The command reads the key from
`TOKEN_SIGNING_KEY`, or from `--key-file` for an operator issuing on a
laptop against a value taken from the Secret, and fails plainly with
neither.

The same console script runs in the container, so in a deployment the
natural place to issue is the pod, where the key is already in the
environment:

```console
$ kubectl exec deploy/workshop-analytics -- workshop-analytics \
    token issue --name showcase-binder \
    --label deployment=showcase-binder --expires 2027-01-31
```

Record the `jti` beside whoever the token went to. It is what you
will need to revoke it.

## Inspecting

`workshop-analytics token inspect <token>` decodes the claims without
the key, so anyone holding a token can see what it says; `-` reads
the token from standard input.

## Revoking before expiry

`DENIED_TOKENS_FILE` names a file of `jti` values, one per line, with
`#` comments. The file is re-read whenever its modification time
changes, so adding a line takes effect without a restart. In a
cluster it is a ConfigMap. A denied token's cookie sessions stop
working too.

## Which labels a token binds

A token's labels are the operator's own statement about the holder,
`deployment=showcase-binder` say, and they are trusted because they
are signed. Labels arriving on events are data: validated for shape
and count, stored, but never believed more than that, because a
public token means anyone can post anything under it. When the two
collide, the token's label stands and the event's own is kept under
the reserved `client.` prefix (`client.deployment`), so nothing a
client sent is lost and nothing a client sends can impersonate the
operator's label.

The label rules are the extension's: keys of lower case letters,
digits, underscore, dot and hyphen up to 63 characters, values up to
128, at most 16 per block. A token that breaks them cannot be issued;
an event that breaks them is rejected on its own, the rest of its
batch still stored.

## Public and private tokens

A token given to a private deployment, a JupyterHub whose
`overrides.json` is in the singleuser image, is a real credential and
stays out of any public repository. A token given to a collection
author or a public deployment, the showcase's `postBuild` in a public
repository say, is visible to anyone, so it is routing and light spam
protection only; the body cap, schema and label validation, dedupe
and the per-token rate limit carry the safety for every token alike.

## How a token travels

A sender puts it in the `Authorization` header as `Bearer <token>`,
which is what the extension does and what keeps it out of every log.
The service also accepts `?token=<token>` on the sink URL, for a
configuration that must stay URL-only, and for a JupyterLite site,
whose browser preflight carries no header; the service's access log
records the path of a request and never its query string, and the
traces mask a `token` parameter, so that form stays out of the logs
too, though a proxy in front of the service keeps its own access log
with whatever it chooses. A missing or bad token on
the sink answers 404, so the URL space reveals nothing; on the API it
answers 401. The reason (no token, expired, wrong scope, wrong key,
revoked) is in the response body and in the service's log as a
warning, never the token itself, so a sender that discards the body,
as the extension does, can still be diagnosed from the service side.
