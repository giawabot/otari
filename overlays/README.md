# NorthRouter billing overlay

Install the package into the Otari environment:

```bash
python -m pip install -e overlays/
```

Configure the overlay in the server environment, not `config.yml`:

```bash
OTARI_BOOTSTRAP=northrouter_billing.overlay:register
NORTHROUTER_BILLING_OAUTH_CLIENT_ID=northrouter-billing
NORTHROUTER_BILLING_SERVICE_TOKEN=...
NORTHROUTER_ORG_CONCURRENCY_LIMIT=10
NORTHROUTER_MIN_AVAILABLE_USD=0
```

`NORTHROUTER_BILLING_SERVICE_TOKEN` is required at startup. Keep it secret and
never put it in logs. The payment website owns payment intake and lifecycle; it
uses the machine API below to grant or reverse funds after a payment decision.

Register the billing website under `oauth_apps` in Otari's `config.yml` with the
client ID matching `NORTHROUTER_BILLING_OAUTH_CLIENT_ID`. The website signs
users in through Otari's PKCE OAuth app flow (`docs/oauth-apps.md`) and receives
a revocable self-service API key tagged with `oauth_client_id`. Active
organization members may read `/context`; only owners and admins may read an
organization ledger.

Run the overlay's independent migration chain against the same database used by
Otari:

```bash
NORTHROUTER_DATABASE_URL=sqlite+aiosqlite:///./otari.db \
  python -m alembic -c overlays/northrouter_billing/alembic.ini upgrade head
```

For PostgreSQL, set `NORTHROUTER_DATABASE_URL` to its Otari database URL. The
overlay uses a separate `nr_alembic_version` table and leaves Otari's migration
history untouched. Migration `0005_payments_leave_the_overlay` removes the
retired payment tables and converts the former event table into the grant
idempotency store.

## Route surface

All routes mount under `/api/v1/nr/billing`, gated on the
`northrouter_billing` capability and authenticated per route:

| Route | Auth |
| --- | --- |
| `GET /context` | Billing OAuth app key (any active organization member) or master key |
| `GET /orgs/{organization_id}/ledger?limit=` | Billing OAuth app key (organization owner/admin) or master key |
| `POST /orgs/{organization_id}/grants` | Billing service bearer token or master key |
| `POST /orgs/{organization_id}/status` | Billing service bearer token or master key |
| `POST /holds/sweep` | Billing service bearer token or master key |

Grant requests contain a signed decimal USD `amount`, a non-empty
`idempotency_key`, and a `reason`. Positive amounts append a `grant`; negative
amounts append a `refund`. The key and ledger update commit together. Replays
return the original amount, ledger entry, and resulting balance without
crediting twice.

Status requests set `status` to `active` or `suspended` and provide a `reason`.
A suspended organization is refused by the billing gate. The web service should
use stable idempotency keys across retries and send status changes only after
its own payment-state decision.

A deployment-paid request for an organization below `NORTHROUTER_MIN_AVAILABLE_USD`
(or with no paid-in funds) is refused by the core with
`402 {"code":"insufficient_funds"}` before dispatch. A served request is
charged into `nr_ledger_entries` as `consume` and updates the maintained
`nr_orgs.balance` transactionally.
