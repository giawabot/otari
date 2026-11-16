# NorthRouter billing overlay

Install the package into the Otari environment:

```bash
python -m pip install -e overlays/
```

Configure the overlay in the server environment, not `config.yml`:

```bash
OTARI_BOOTSTRAP=northrouter_billing.overlay:register
NORTHROUTER_BILLING_OAUTH_CLIENT_ID=northrouter-billing
NORTHROUTER_STRIPE_SECRET_KEY=...
NORTHROUTER_STRIPE_WEBHOOK_SECRET=...
NORTHROUTER_STRIPE_PRICE_STARTER=...
NORTHROUTER_STRIPE_PRICE_GROWTH=...
NORTHROUTER_STRIPE_PRICE_SCALE=...
NORTHROUTER_ETRANSFER_FX_MARGIN=0
NORTHROUTER_ORG_CONCURRENCY_LIMIT=10
NORTHROUTER_MIN_AVAILABLE_USD=0
```

Register the billing website under `oauth_apps` in Otari's `config.yml` with the
client ID matching `NORTHROUTER_BILLING_OAUTH_CLIENT_ID`. The website signs
users in through Otari's PKCE OAuth app flow (`docs/oauth-apps.md`) and receives
a revocable self-service API key tagged with `oauth_client_id`. Only an active
organization owner or admin holding a key issued to that OAuth client (or the
deployment master key) may access organization billing routes. Operator routes
use Otari's `require_deployment_operator` gate and record the acting operator
identity (`email`, user UUID, or `master-key`) in `reviewed_by`.

Stripe API calls pin version `2025-03-31.basil`. Configure the Stripe webhook
endpoint in the Stripe dashboard to use API version `2025-03-31.basil` so
webhook payloads match the SDK contract. Stripe credentials are used only by
the billing HTTP routes and webhook handler; never put card data or secrets in
logs.

E-transfers are received in CAD (`CA$100`, `CA$500`, or `CA$1000`) and converted
to USD at approval time using the Bank of Canada daily `FXUSDCAD` series
(`https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1`). The
credited USD amount is `cad / rate * (1 - NORTHROUTER_ETRANSFER_FX_MARGIN)`,
rounded down to cents, and recorded alongside the rate, observation date,
source (`boc:FXUSDCAD`), and margin.

Run the overlay's independent migration chain against the same database used by
Otari:

```bash
NORTHROUTER_DATABASE_URL=sqlite+aiosqlite:///./otari.db \
  python -m alembic -c overlays/northrouter_billing/alembic.ini upgrade head
```

For PostgreSQL, set `NORTHROUTER_DATABASE_URL` to its Otari database URL. The
overlay uses a separate `nr_alembic_version` table and leaves Otari's migration
history untouched.

Migrations `0003_billing_hardening` and `0004_billing_fx_disputes_balance`
enforce unique `stripe_customer_id` values on `nr_orgs` and globally unique
normalized e-transfer references (`LOWER(TRIM(reference))`) across all
organizations on `nr_e_transfers`. Run these pre-check queries before upgrading:

```sql
SELECT LOWER(TRIM(reference)) AS normalized_reference, COUNT(*)
FROM nr_e_transfers
GROUP BY LOWER(TRIM(reference))
HAVING COUNT(*) > 1;

SELECT stripe_customer_id, COUNT(*)
FROM nr_orgs
WHERE stripe_customer_id IS NOT NULL
GROUP BY stripe_customer_id
HAVING COUNT(*) > 1;
```

## Route surface

All routes mount under `/api/v1/nr/billing`, gated on the
`northrouter_billing` capability, authenticated per route:

| Route | Auth |
| --- | --- |
| `GET /context` | Billing OAuth app key (any active org member) or master key |
| `POST /orgs/{organization_id}/top-up` | Billing OAuth app key (org owner/admin) or master key |
| `POST /orgs/{organization_id}/subscriptions` | Billing OAuth app key (org owner/admin) or master key |
| `GET /orgs/{organization_id}/subscriptions` | Billing OAuth app key (org owner/admin) or master key |
| `POST /orgs/{organization_id}/e-transfers` | Billing OAuth app key (org owner/admin) or master key |
| `GET /orgs/{organization_id}/ledger?limit=` | Billing OAuth app key (org owner/admin) or master key |
| `POST /webhooks/stripe` | Public; Stripe signature verified, event-id idempotent |
| `GET /e-transfers?status=pending&limit=` | Deployment operator (`require_deployment_operator`) |
| `POST /e-transfers/{transfer_id}/approve` | Deployment operator (`require_deployment_operator`) |
| `POST /e-transfers/{transfer_id}/reject` | Deployment operator (`require_deployment_operator`) |
| `POST /holds/sweep` | Deployment operator (`require_deployment_operator`) |

A deployment-paid request for an org below `NORTHROUTER_MIN_AVAILABLE_USD` (or
with no paid-in funds) is refused by the core with
`402 {"code":"insufficient_funds"}` before dispatch; a served request is
charged into `nr_ledger_entries` as `consume` and updates `nr_orgs.balance`
transactionally.
