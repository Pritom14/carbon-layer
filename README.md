# Carbon Layer

Chaos engineering for payment flows.

Every company processing payments tests the happy path — payment succeeds, order fulfilled — and ships. What breaks in production is everything else: dispute spikes your system doesn't respond to, refund storms that break reconciliation, gateway errors that leave orders stuck, webhook sequences your handlers were never tested against.

Carbon Layer lets you simulate these failure modes against your own integration before your customers encounter them. Run a scenario, point it at your webhook endpoint, and see exactly what your system handles and what it doesn't.

---

## Installation

```bash
pip install carbon-layer
```

PostgreSQL is required for storage. Create a database before the first run:

```bash
createdb carbon
```

Or use Docker:

```bash
docker run -d --name carbon-pg \
  -e POSTGRES_PASSWORD=carbon \
  -e POSTGRES_DB=carbon \
  -p 5433:5432 postgres:15
```

Set the connection string via environment variable or `.env` file:

```
DATABASE_URL=postgresql://postgres:carbon@localhost:5433/carbon
```

---

## Quickstart

No Razorpay account needed to get started. The mock adapter simulates the full payment lifecycle locally.

List available scenarios:

```bash
carbon scenarios-list
```

Run a scenario:

```bash
carbon run dispute-spike --provider mock
carbon report --run-id <run_id>
```

---

## Webhook Simulation

The real value of Carbon Layer is testing your webhook handlers. Point it at your endpoint and it fires Razorpay-format events — `payment.captured`, `dispute.created`, `refund.processed`, and more — after the scenario runs. Payloads are signed with `X-Razorpay-Signature` (HMAC-SHA256), the same as Razorpay's live webhooks.

```bash
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks/razorpay
```

The report shows how your endpoint responded for each event type — 2xx, 4xx, 5xx, or timeout. No Razorpay account required.

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
  --webhook-url http://localhost:8000/webhooks/razorpay \
  --callback-url http://localhost:8000/carbon/results
```

The callback payload includes pass/fail status, findings summary, and webhook delivery counts. Your pipeline can fail the build if `passed` is false.

---

## Using with Razorpay

To run scenarios against your Razorpay test account:

```bash
carbon run dispute-spike \
  --provider razorpay \
  --api-key rzp_test_xxx \
  --api-secret yyy \
  --webhook-url https://your-staging-app.com/webhooks/razorpay
```

Or set credentials via environment variables: `RAZORPAY_API_KEY` and `RAZORPAY_API_SECRET`.

Note: Razorpay's test API does not support server-side payment creation or dispute creation. Scenarios that require these (e.g. `dispute-spike`) use the mock adapter automatically for those actions. Use `--provider mock` if you don't have Razorpay test credentials.

---

## License

Apache 2.0 — see [LICENSE](./LICENSE).
