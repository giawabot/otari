# NorthRouter billing overlay

Install the package into the Otari environment:

```bash
/home/giawa/otari-billing/.venv/bin/python -m pip install -e /home/giawa/northrouter/otari_overlays
```

Configure the overlay in the server environment, not `config.yml`:

```bash
OTARI_BOOTSTRAP=northrouter_billing.overlay:register
NORTHROUTER_STRIPE_SECRET_KEY=...
NORTHROUTER_STRIPE_WEBHOOK_SECRET=...
NORTHROUTER_STRIPE_PRICE_STARTER=...
NORTHROUTER_STRIPE_PRICE_GROWTH=...
NORTHROUTER_STRIPE_PRICE_SCALE=...
NORTHROUTER_BILLING_OPERATOR_TOKEN=...
```

The operator token protects e-transfer approval and rejection. Stripe credentials
are used only by the billing HTTP routes and webhook handler. Never put card data
or these secrets in logs.

Run the overlay's independent migration chain against the same database used by
Otari:

```bash
NORTHROUTER_DATABASE_URL=sqlite+aiosqlite:///./otari.db \
  /home/giawa/otari-billing/.venv/bin/python -m alembic \
  -c /home/giawa/northrouter/otari_overlays/northrouter_billing/alembic.ini upgrade head
```

For PostgreSQL, set `NORTHROUTER_DATABASE_URL` to its Otari database URL. The
overlay uses a separate `nr_alembic_version` table and leaves Otari's migration
history untouched.

## Route surface (plan 03's contract)

All routes mount under `/api/v1/nr/billing`, gated on the
`northrouter_billing` capability, authenticated per route:

| Route | Auth |
| --- | --- |
| `POST /orgs/{org}/top-up` | API key (scoped to its org) or master key |
| `POST /orgs/{org}/subscriptions` | API key or master key |
| `GET /orgs/{org}/subscriptions` | API key or master key |
| `POST /webhooks/stripe` | public; Stripe signature verified, event-id idempotent |
| `POST /e-transfers` | API key or master key |
| `POST /e-transfers/{id}/approve` | `NorthRouter-Operator-Token` header |
| `POST /e-transfers/{id}/reject` | `NorthRouter-Operator-Token` header |
| `GET /orgs/{org}/ledger?limit=` | API key or master key |

A deployment-paid request for an org with no paid-in funds is refused by the
core with `402 {"code":"insufficient_funds"}` before dispatch; a served request
is charged (overdraft tolerated) into `nr_ledger_entries` as `consume`.
