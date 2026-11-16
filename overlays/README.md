# NorthRouter billing overlay

The overlay keeps a prepaid USD balance per Otari organization and gates
deployment-paid requests on it. Payment intake (cards, e-transfers, invoices)
lives in the NorthRouter web service, which credits or reverses funds and
suspends or reactivates organizations through the machine API below.

Install the package into the Otari environment:

```bash
python -m pip install -e overlays/
```

## Configuration

Configure the overlay in the server environment, not `config.yml`:

```bash
OTARI_BOOTSTRAP=northrouter_billing.overlay:register
OTARI_DEPLOYMENT_PAID_JOBS=false
NORTHROUTER_BILLING_SERVICE_TOKEN=...            # required, at least 32 characters
NORTHROUTER_BILLING_SERVICE_TOKEN_PREVIOUS=...   # optional, during a rotation
NORTHROUTER_BILLING_OAUTH_CLIENT_ID=northrouter-billing
NORTHROUTER_ORG_CONCURRENCY_LIMIT=8
NORTHROUTER_MIN_AVAILABLE_USD=1.00
NORTHROUTER_MAX_GRANT_USD=10000
```

| Variable | Meaning |
| --- | --- |
| `NORTHROUTER_BILLING_SERVICE_TOKEN` | Bearer token the web service authenticates with. Required at startup and at least 32 characters; generate it with `openssl rand -base64 48`. It can credit any amount up to the grant cap, so keep it in a secret store and never log it. |
| `NORTHROUTER_BILLING_SERVICE_TOKEN_PREVIOUS` | A second accepted token, so the service token can rotate without downtime. See [Rotating the service token](#rotating-the-service-token). |
| `NORTHROUTER_BILLING_OAUTH_CLIENT_ID` | The `oauth_apps` client whose keys may read billing for their organization. Unset, only the service token and the master key read. |
| `NORTHROUTER_ORG_CONCURRENCY_LIMIT` | Deployment-paid model calls one organization may have in flight at once. Defaults to 8. |
| `NORTHROUTER_MIN_AVAILABLE_USD` | Balance below which deployment-paid requests are refused with `402 insufficient_funds`. Defaults to 1.00. |
| `NORTHROUTER_MAX_GRANT_USD` | Largest single grant or reversal accepted. Defaults to 10000. |

`OTARI_DEPLOYMENT_PAID_JOBS=false` is a core setting, not the overlay's. Leave
it out and the overlay logs a warning at startup: a batch or video job on a
hosted provider is charged only when its results are accounted, so the balance
check cannot bound what one job costs.

### How far a balance can go negative

Requests are admitted on the balance alone and charged when they settle, so
concurrent requests can take an organization below zero. The exposure is about
`NORTHROUTER_ORG_CONCURRENCY_LIMIT` times the most expensive single request,
from a balance at the floor. Direct gateway tool calls (web search, code
execution) take no concurrency slot. Raise the floor or lower the limit if the
models you host cost more per request.

### Rotating the service token

1. Set `NORTHROUTER_BILLING_SERVICE_TOKEN_PREVIOUS` to the current token and
   `NORTHROUTER_BILLING_SERVICE_TOKEN` to the new one; restart Otari.
2. Switch the web service to the new token.
3. Unset `NORTHROUTER_BILLING_SERVICE_TOKEN_PREVIOUS`; restart Otari.

Grants and status events record which token authorized them (`service`,
`service_previous` or `master`).

## Billing website sign-in

Register the billing website under `oauth_apps` in Otari's `config.yml` with
the client ID matching `NORTHROUTER_BILLING_OAUTH_CLIENT_ID`. The website signs
users in through Otari's PKCE OAuth app flow (`docs/oauth-apps.md`) and
receives a revocable self-service API key. Otari records the issuing app on the
key in a column only the app flow writes; a key a member mints for themselves
is refused even if its `metadata` names the app. Keys issued before that column
existed are refused until the user signs in to the website again.

## Database

Run the overlay's migration chain against the same database Otari uses:

```bash
NORTHROUTER_DATABASE_URL=sqlite+aiosqlite:///./otari.db \
  python -m alembic -c overlays/northrouter_billing/alembic.ini upgrade head
```

For PostgreSQL, set `NORTHROUTER_DATABASE_URL` to Otari's database URL. The
overlay uses its own `nr_alembic_version` table, and autogenerate only ever
compares `nr_*` tables, so it never proposes changes to Otari's schema.

The chain was reset to a single `nr_0001_initial` revision before the overlay
was first deployed. A development database migrated with an earlier revision
needs its `nr_*` tables and `nr_alembic_version` dropped once before
`upgrade head`; `overlays/scripts/reset_dev_db.py` does exactly that (dry run
by default, `--yes` to apply, `nr_*` tables only — never Otari's).

## Route surface

All routes mount under `/api/v1/nr/billing`, gated on the `northrouter_billing`
capability:

| Route | Auth |
| --- | --- |
| `POST /orgs/{organization_id}/grants` | Service token or master key |
| `POST /orgs/{organization_id}/status` | Service token or master key |
| `POST /holds/sweep` | Service token or master key |
| `GET /orgs/{organization_id}/account` | Service token, master key, or the billing app's key of an organization owner/admin |
| `GET /orgs/{organization_id}/ledger?limit=&cursor=` | Service token, master key, or the billing app's key of an organization owner/admin |
| `GET /context` | The billing app's key (any active organization member) or master key |

The master key is accepted only in a header; a dashboard session never reaches
these routes.

### Grants

A grant has a signed decimal USD `amount`, a non-empty `idempotency_key`, a
`reason`, and an optional `source_ref` (the web service's own payment id).
Positive amounts append a `grant`; negative amounts append a `refund` and may
take the balance below zero. The key and ledger update commit together. A
replay returns the original amount, ledger entry and resulting balance without
crediting twice; reusing a key with a different payload is a `409`. Amounts
beyond `NORTHROUTER_MAX_GRANT_USD` either way are a `422`.

The `reason` is shown to the organization's owners and admins in their ledger,
so keep internal notes in `source_ref` or in the web service.

### Status

A status request sets `status` to `active` or `suspended` with a `reason` and
the `effective_at` time the web service made the decision (timezone required,
at most five minutes ahead of Otari's clock). A decision older than the one in
force is recorded and ignored, so a delayed retry cannot undo a newer decision;
the response's `applied` says which happened. Every decision is kept in
`nr_status_events`. A suspended organization is refused by the billing gate.

### Reads

`/account` returns the status, balance, held, available and floor; the service
token and master key also see the suspension reason and when the status took
effect. `/ledger` pages newest first, without hold and release rows: pass
`next_cursor` back as `cursor`. The service token and master key also see each
entry's `ref`.

### Hold sweep

Holds expire after an hour. `POST /holds/sweep` releases up to 500 expired
holds per call and returns `more: true` while more remain; run it on a
schedule.
