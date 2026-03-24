# Carbon Layer

[![PyPI](https://img.shields.io/pypi/v/carbon-layer)](https://pypi.org/project/carbon-layer/)
[![CI](https://github.com/Pritom14/carbon-layer/actions/workflows/ci.yml/badge.svg)](https://github.com/Pritom14/carbon-layer/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/carbon-layer)](https://pypi.org/project/carbon-layer/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Chaos engineering for payment flows.

Every company processing payments tests the happy path — payment succeeds, order fulfilled — and ships. What breaks in production is everything else: dispute spikes your system doesn't respond to, refund storms that break reconciliation, gateway errors that leave orders stuck, webhook sequences your handlers were never tested against.

Carbon Layer lets you simulate these failure modes against your own integration before your customers encounter them. Run a scenario, point it at your webhook endpoint, and see exactly what your system handles and what it doesn't.

---

## Installation

### Option A: Quick start (no database needed)

```bash
pip install carbon-layer
```

Works out of the box. Carbon Layer uses SQLite by default — data is stored in `~/.carbon/carbon.db`. Nothing else to install or configure.

### Option B: With PostgreSQL (if you already have it)

```bash
pip install carbon-layer[postgres]
```

Then set your connection string via a `.env` file or environment variable:

```bash
# .env file in your project directory
DATABASE_URL=postgresql://postgres:yourpassword@localhost:5432/carbon
```

Or with Docker:

```bash
docker run -d --name carbon-pg \
  -e POSTGRES_PASSWORD=carbon \
  -e POSTGRES_DB=carbon \
  -p 5432:5432 postgres:15

# then set:
DATABASE_URL=postgresql://postgres:carbon@localhost:5432/carbon
```

Both options support all features. You can start with SQLite and switch to PostgreSQL anytime by installing the extra and setting `DATABASE_URL`.

### Option C: With pipx (isolated install)

```bash
pipx install carbon-layer
```

### Option D: With Docker

```bash
docker run ghcr.io/pritom14/carbon-layer run dispute-spike --provider mock
```

---

## Quickstart

```bash
# List available scenarios
carbon scenarios-list

# Run your first scenario
carbon run dispute-spike --provider mock

# View the report (use the run_id from the output above)
carbon report --run-id <run_id>
```

No payment gateway account needed. The mock adapter simulates the full payment lifecycle locally.

---

## Webhook Simulation

The real value of Carbon Layer is testing your webhook handlers. Point it at your endpoint and it fires provider-specific webhook events — `payment.captured`, `payment_intent.succeeded`, `PAYMENT_SUCCESS_WEBHOOK`, `ORDER_SUCCEEDED`, and more — after the scenario runs. Payloads are signed exactly like real webhooks from each provider (Razorpay, Stripe, Cashfree, Juspay).

```bash
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks
```

The report shows how your endpoint responded for each event type — 2xx, 4xx, 5xx, or timeout. No payment gateway account required.

---

## Scenarios

| Scenario | What it tests |
|----------|---------------|
| `dispute-spike` | 15% dispute rate — does your system respond and submit evidence? |
| `payment-decline-spike` | 30% payment failure rate — does your retry and order state logic hold? |
| `refund-storm` | Mass refunds on captured payments — does reconciliation break? |
| `flash-sale` | High order and payment volume — does throughput hold? |
| `gateway-error-burst` | Intermittent gateway errors — are orders left in inconsistent state? |
| `min-amount` | Minimum paise transactions — are edge-case amounts handled correctly? |
| `max-amount` | Large-value transactions — are limits and approvals handled correctly? |
| `upi-timeout` | UPI payments stuck without terminal status — does your reconciliation catch it? |
| `vpa-not-found` | Invalid UPI VPA failures — does your handler distinguish VPA errors from general failures? |
| `mandate-rejection` | UPI autopay mandate rejections — does your handler notify the customer? |
| `settlement-delay` | Refunds on captured-but-unsettled payments — does reconciliation handle it? |

---

## Parameter Overrides

Override scenario parameters at runtime without editing YAML:

```bash
carbon run dispute-spike --provider mock --set baseline_orders=500 --set dispute_rate=0.3
```

Use `--set` multiple times for multiple parameters. Unknown keys are ignored with a warning.

---

## HTML Reports

Export a shareable HTML report after any run:

```bash
carbon report --run-id <run_id> --format html
```

Writes `carbon_report_<run_id>.html` to the current directory. Self-contained, no external dependencies — safe to share with your team or attach to an incident report.

---

## Webhook Resilience Testing

Test how your webhook handler behaves under real-world failure conditions.

### Idempotency (duplicate webhooks)

Fire each webhook multiple times to test whether your handler processes duplicates:

```bash
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks --webhook-repeat 5
```

### Out-of-order delivery

Gateways don't guarantee webhook order. Test with randomized or reversed delivery:

```bash
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks --webhook-order random
```

Options: `sequence` (default), `reverse`, `random`.

### Signature verification

Test whether your handler actually validates webhook signatures:

```bash
# Missing signatures — should your handler reject these?
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks --webhook-signature missing

# Corrupted signatures
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks --webhook-signature corrupted

# Signed with wrong secret
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks --webhook-signature wrong_secret
```

Options: `valid` (default), `missing`, `corrupted`, `wrong_secret`.

---

## Webhook Replay

Replay stored webhook payloads from any previous run. Useful for regression testing after code changes:

```bash
carbon replay <run_id> --webhook-url http://localhost:8000/webhooks
```

---

## CI/CD Integration

Use `--ci` to fail the build if any webhook returned 5xx or timed out:

```bash
carbon run dispute-spike --provider mock \
  --webhook-url http://localhost:8000/webhooks \
  --ci
```

Use `--callback-url` to POST a JSON run summary to your pipeline:

```bash
carbon run dispute-spike --provider mock \
  --webhook-url http://localhost:8000/webhooks \
  --callback-url http://localhost:8000/carbon/results \
  --ci
```

The callback payload includes pass/fail status, findings summary, and webhook delivery counts.

### JSON output

Pipe results to `jq`, Datadog, Slack, or any tool:

```bash
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks --output json
```

Returns structured JSON with run status, findings, webhook delivery stats, and overall pass/fail.

### Example GitHub Actions workflow

See [`examples/github-actions.yml`](examples/github-actions.yml) for a copy-paste workflow that runs Carbon Layer on every PR.

---

## Supported Payment Gateways

Carbon Layer generates provider-specific webhook payloads with correct signing for each gateway.

| Provider | Webhook Format | Signing | CLI Flag |
|----------|---------------|---------|----------|
| Mock | Razorpay-format | HMAC-SHA256 | `--provider mock` |
| Razorpay | `X-Razorpay-Signature` | HMAC-SHA256 | `--provider razorpay` |
| Stripe | `Stripe-Signature: t=...,v1=...` | HMAC-SHA256 | `--provider stripe` |
| Cashfree | `x-webhook-signature` | Base64(HMAC-SHA256) | `--provider cashfree` |
| Juspay | `Authorization: Basic ...` | Basic Auth | `--provider juspay` |

### Razorpay

```bash
carbon run dispute-spike \
  --provider razorpay \
  --api-key your_test_key \
  --api-secret your_test_secret \
  --webhook-url https://your-app.com/webhooks
```

Or set `RAZORPAY_API_KEY` and `RAZORPAY_API_SECRET` as environment variables.

### Stripe

```bash
carbon run dispute-spike \
  --provider stripe \
  --stripe-key sk_test_xxx \
  --webhook-url https://your-app.com/webhooks
```

Or set `STRIPE_API_KEY` as an environment variable.

### Cashfree

```bash
carbon run dispute-spike \
  --provider cashfree \
  --cashfree-id your_app_id \
  --cashfree-secret your_secret_key \
  --webhook-url https://your-app.com/webhooks
```

Or set `CASHFREE_CLIENT_ID` and `CASHFREE_CLIENT_SECRET` as environment variables.

### Juspay

```bash
carbon run dispute-spike \
  --provider juspay \
  --juspay-key your_api_key \
  --juspay-merchant-id your_merchant_id \
  --webhook-url https://your-app.com/webhooks
```

Or set `JUSPAY_API_KEY` and `JUSPAY_MERCHANT_ID` as environment variables.

### Mock (no credentials needed)

```bash
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks
```

Mock mode simulates the full payment lifecycle locally. Use this if you don't have test credentials — all 11 scenarios work out of the box.

---

## Carbon Layer Pro (coming soon)

The open-source CLI covers 11 scenarios, 5 payment gateways, and webhook resilience testing. We're building a hosted Pro tier for teams that need more:

- **Scheduled runs** — run scenarios on a cron, get notified when your handlers regress
- **PDF reports** — export compliance-ready reports for audits and stakeholders
- **Compliance dashboards** — track webhook handler reliability over time
- **Pro scenarios** — RBI compliance checks, PCI readiness suite, 10k TPS load tests, multi-gateway failover, settlement reconciliation
- **Team management** — shared runs, role-based access, audit logs

The free CLI stays free and open-source (Apache 2.0). Pro is a separate hosted product.

Join the waitlist: [pritom14.github.io/carbon-layer/waitlist](https://pritom14.github.io/carbon-layer/waitlist)

---

## License

Apache 2.0 — see [LICENSE](./LICENSE).
