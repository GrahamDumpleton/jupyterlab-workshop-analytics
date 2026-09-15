# Deploying

The service runs as one container: FastAPI on uvicorn with a SQLite
database on a volume, migrated at start, holding one secret, the key
every token is signed with. This page covers the image, running it
with Docker and on Kubernetes, the key and the tokens, observability,
and then, host by host, what a workshop deployment puts in its
settings to report here.

## The image

The image is published on GitHub Container Registry as

```
ghcr.io/grahamdumpleton/jupyterlab-workshop-analytics
```

tagged with the version, `0.1.0` say, and as `latest`, for
`linux/amd64` and `linux/arm64`. It is built from `deploy/Dockerfile`
by `.github/workflows/image.yml` when a bare version tag is pushed;
the workflow refuses a tag that does not match the version in
`pyproject.toml`. A release is therefore: set the version in
`pyproject.toml`, commit, push, wait for `ci` to pass, then

```console
$ git tag 0.2.0
$ git push origin 0.2.0
```

and the `image` workflow publishes it. After the first publish, check
the package's visibility on GitHub: a package linked to a public
repository is meant to inherit its visibility, and a private one
needs a pull secret in every cluster that runs it.

Inside the image the package is installed into `/app/.venv` from the
lock file, without the development tools, running as a non-root user
with `/data` as the volume, `/etc/workshop-analytics/wrapture.toml`
as the deployed observability configuration, port 8080 exposed, a
health check on `/healthz`, and `workshop-analytics serve` as the
command. `DATABASE_URL` defaults to `sqlite:////data/analytics.db`.
Every CLI command is in the image, so tokens are issued and files
imported in the running container, where the key and the database
already are.

`just image` builds the same image locally as
`jupyterlab-workshop-analytics:local`, and `just image-run` runs it on
port 8080 with a generated key and a named volume.

## With Docker

```console
$ export TOKEN_SIGNING_KEY=$(docker run --rm ghcr.io/grahamdumpleton/jupyterlab-workshop-analytics workshop-analytics key generate)
$ docker run -d --name analytics -p 8080:8080 \
    -v analytics-data:/data \
    -e TOKEN_SIGNING_KEY \
    ghcr.io/grahamdumpleton/jupyterlab-workshop-analytics
$ docker exec analytics workshop-analytics token issue --name me --expires 30d --scope dashboard
```

Keep the key somewhere safe: it is the one thing that cannot be
regenerated without invalidating every token. Add
`-e WRAPTURE_CONFIG=/etc/workshop-analytics/wrapture.toml` and
`-e OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318` to
send traces to an otel-desktop-viewer on the host. Every setting is
an environment variable; the full list is on
[development](development.md#settings).

## On Kubernetes

`deploy/kubernetes/` is a kustomize base and two overlays.

The base is a Namespace `workshop-analytics`, a PersistentVolumeClaim
(ReadWriteOnce, 1Gi), a Deployment with one replica and `strategy:
Recreate`, since SQLite on one volume cannot be shared and the old
pod must be gone before the new one opens the database, a Service on
port 80, and a ConfigMap for the deny list generated from
`base/denied.txt`. The Deployment runs as a non-root user with no
capabilities, sets `DATABASE_URL`, `WRAPTURE_CONFIG`,
`DENIED_TOKENS_FILE` and `COOKIE_SECURE=true`, takes
`TOKEN_SIGNING_KEY` from a Secret named `workshop-analytics-key`, and
probes `/healthz`. The Secret is not in the base: each overlay
generates it from a `key.env` file beside it, so the key never sits
in a manifest.

**The local overlay**, for a kind or Docker Desktop cluster, uses the
image built locally, runs otel-desktop-viewer beside the service so
the same viewer works in the cluster, points the service at it, turns
the cookie's Secure flag off for plain HTTP, and has no Ingress:

```console
$ just image
$ kind load docker-image jupyterlab-workshop-analytics:local
$ uv run workshop-analytics key generate      # put it in overlays/local/key.env
$ kubectl apply -k deploy/kubernetes/overlays/local
$ kubectl -n workshop-analytics port-forward svc/workshop-analytics 8080:80
$ kubectl -n workshop-analytics port-forward svc/otel-desktop-viewer 8000:8000
```

The `key.env` in the local overlay holds a placeholder that does not
decode, so the service refuses to start until it is replaced.

**The production overlay** pins the published image at a version,
generates the Secret from a `key.env` that git ignores (copy
`key.env.example`), sets the collector endpoint, adds resource
requests and limits and a larger volume, and adds an Ingress with TLS
through a cert-manager annotation. Edit the host in `ingress.yaml`,
the issuer, the collector endpoint and the image tag, then:

```console
$ kubectl apply -k deploy/kubernetes/overlays/production
```

The Ingress is what a service receiving from Binder, from a
JupyterLite site or from another cluster needs. A service co-located
with a JupyterHub and fed over the cluster network can leave it out:
the singleuser pods post to
`http://workshop-analytics.workshop-analytics.svc/events`, events
never leave the cluster, and the dashboard is reached through a
port-forward or an internal route.

`just manifests local` and `just manifests production` render an
overlay without applying it.

### The key and the tokens

Generate the key once, on a laptop, and put it in the overlay's
`key.env`:

```console
$ uv run workshop-analytics key generate
```

Issue tokens in the pod, where the key already is:

```console
$ kubectl -n workshop-analytics exec deploy/workshop-analytics -- \
    workshop-analytics token issue --name hub --expires 2027-01-31 \
    --label deployment=hub
```

with `--scope dashboard` for a supervisor and `--scope api` for an
analyst or an assistant. Record each token's `jti` beside whoever it
went to. To revoke one before it expires, add its `jti` to
`base/denied.txt` and apply again; the ConfigMap updates in the pod
within a minute and the service re-reads the file. Rotating the key
in `key.env` and applying invalidates every token at once; that is
the break-glass move. The whole model is on [tokens](tokens.md).

### Observability

The image carries `deploy/wrapture.toml` at
`/etc/workshop-analytics/wrapture.toml`: the repository's
configuration without the printer sink, exporting traces, metrics and
logs by OTLP to whatever `OTEL_EXPORTER_OTLP_ENDPOINT` names. The
overlays set that to the viewer or the cluster's collector. A cluster
that wants different tracing mounts its own ConfigMap over the path.
What the configuration records, and what it leaves out, is on
[observability](observability.md).

### Later: PostgreSQL

The store is SQLAlchemy Core and the migrations are Alembic, so
PostgreSQL is a `DATABASE_URL` change with the `postgres` extra
installed in the image. An overlay for it drops the volume, takes the
URL from a Secret and can raise the replica count once the in-process
broadcaster is replaced by `LISTEN`/`NOTIFY`. The SQL tool then wants
a read-only role for the URL's user. None of that is built yet.

## Pointing workshops at the service

A workshop deployment reports by setting the `analytics` block in the
extension's settings, in `overrides.json` under the JupyterLab
settings directory, which applies to every workshop without asking.
The block's shape is the same everywhere; what differs by host is
where the file goes, whether the token is secret, whether learners
have an identity, and how the batch reaches the service. The
extension's own pages say more about the block ([reporting
progress](https://jupyterlab-workshop.readthedocs.io/en/latest/deploying.html#reporting-progress)
and [progress events](https://jupyterlab-workshop.readthedocs.io/en/latest/analytics.html)).

### JupyterHub

The user's `jupyter_server` posts from inside the hub, so the sink can
be the cluster-local Service and the token stays private in the
singleuser image or a shared settings directory. This is the one host
with an identity: `identity: hub` adds the JupyterHub user name to
every event, and the learners report and the drill-down show names.

```json
{
  "@jupyterlab-workshop/labextension:panel": {
    "analytics": {
      "sink": "http://workshop-analytics.workshop-analytics.svc/events",
      "token": "<token issued with --label deployment=hub>",
      "labels": { "course": "intro-git", "term": "2026-s2" },
      "identity": "hub"
    }
  }
}
```

Issue the token with `--label deployment=hub`, and add course and term
as labels in the block or on the token; a token label is trusted, a
block label is data.

### mybinder.org

The pod's `jupyter_server` posts, so no CORS is involved, but the
`overrides.json` is written by `binder/postBuild` in a public
repository, so the token is public by construction. It is routing and
light spam protection only; issue it with a label naming the
deployment and an expiry at the end of the season, and rotate it
when the season ends. Sessions are anonymous, and a learner who walks
away reads as `lost`. The service must be reachable from the public
internet, so this host needs the Ingress.

```json
{
  "@jupyterlab-workshop/labextension:panel": {
    "analytics": {
      "sink": "https://analytics.example.org/events",
      "token": "<public token issued with --label deployment=showcase-binder>",
      "labels": { "collection": "showcase" }
    }
  }
}
```

### GitHub Codespaces

The `jupyter_server` in the codespace posts, from the devcontainer
image's `overrides.json`, so the token is private to the repository.
No identity today. Events carry `host: codespaces`, so a stopped and
restarted codespace shows as a resumed chain rather than a loss.

```json
{
  "@jupyterlab-workshop/labextension:panel": {
    "analytics": {
      "sink": "https://analytics.example.org/events",
      "token": "<token issued with --label deployment=codespaces>",
      "labels": { "repo": "org/workshops" }
    }
  }
}
```

### JupyterLite

The browser itself posts, cross-origin, from an `overrides.json` baked
into the site build, so the token is public and the request carries
no header on its preflight. Two things follow: put the token on the
sink URL as `?token=`, and issue it with `--origin` naming the site,
so the service answers the browser's preflight for that origin. Or
set `ALLOWED_ORIGINS` on the service for every token at once. Events
carry `frontend: jupyterlite`, `host: static` and `platform:
emscripten`.

```json
{
  "@jupyterlab-workshop/labextension:panel": {
    "analytics": {
      "sink": "https://analytics.example.org/events?token=<public token issued with --origin https://workshops.example.org>",
      "labels": { "site": "workshops" }
    }
  }
}
```

### A standalone JupyterLab

The local `jupyter_server` posts, from the user's own settings or an
install's `overrides.json`, with whatever token the user was given.
Events carry `host: local` and the user's operating system as
`platform`. This is also the shape for a laptop pointing at a service
on the same machine, with the sink at `http://127.0.0.1:8080/events`.

```json
{
  "@jupyterlab-workshop/labextension:panel": {
    "analytics": {
      "sink": "https://analytics.example.org/events",
      "token": "<token>",
      "labels": { "cohort": "self-paced" }
    }
  }
}
```

### Which is which in the data

Every event names its host and frontend, so the service tells these
apart as facts: `host` is `jupyterhub`, `binder`, `codespaces`,
`static` or `local`, and `frontend` is `jupyterlab` or `jupyterlite`.
Labels are what the deployment chose to say. `describe` reports the
values seen and, per host and per token, whether sessions carry an
identity, so a report knows which questions it can answer.
