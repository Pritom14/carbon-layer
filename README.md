# Carbon Layer

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

The real value of Carbon Layer is testing your webhook handlers. Point it at your endpoint and it fires payment gateway events — `payment.captured`, `dispute.created`, `refund.processed`, and more — after the scenario runs. Payloads are signed with HMAC-SHA256, matching real payment gateway webhook formats.

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

## CI/CD Integration

Use `--callback-url` to POST a JSON run summary to your pipeline after a scenario completes:

```bash
carbon run dispute-spike --provider mock \
  --webhook-url http://localhost:8000/webhooks \
  --callback-url http://localhost:8000/carbon/results
```

The callback payload includes pass/fail status, findings summary, and webhook delivery counts. Your pipeline can fail the build if `passed` is false.

---

## Using with a Payment Gateway

To run scenarios against your payment gateway's test environment, pass your credentials:

```bash
carbon run dispute-spike \
  --provider razorpay \
  --api-key your_test_key \
  --api-secret your_test_secret \
  --webhook-url https://your-staging-app.com/webhooks
```

Or set credentials via environment variables. Use `--provider mock` if you don't have test credentials — mock mode simulates the full payment lifecycle locally.

---

## License

Apache 2.0 — see [LICENSE](./LICENSE).
