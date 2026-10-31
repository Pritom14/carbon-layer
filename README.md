# Carbon Layer

[![PyPI](https://img.shields.io/pypi/v/carbon-layer)](https://pypi.org/project/carbon-layer/)
[![CI](https://github.com/Pritom14/carbon-layer/actions/workflows/ci.yml/badge.svg)](https://github.com/Pritom14/carbon-layer/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/carbon-layer)](https://pypi.org/project/carbon-layer/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Chaos engineering for payment flows.

Every company processing payments tests the happy path — payment succeeds, order fulfilled — and ships. What breaks in production is everything else: dispute spikes your system doesn't respond to, refund storms that break reconciliation, gateway errors that leave orders stuck, webhook sequences your handlers were never tested against.

Carbon Layer lets you simulate these failure modes against your own integration before your customers encounter them. Run a scenario, point it at your webhook endpoint, and see exactly what your system handles and what it doesn't.

### Does your handler credit a payment twice?

Razorpay, Stripe and Cashfree all deliver webhooks *at least once*, so the same `payment.captured` can arrive more than once. A handler that doesn't dedupe returns `200` every time — so status codes alone will never show the bug. Carbon Layer checks the side effect itself:

```bash
carbon run dispute-spike --provider razorpay \
  --webhook-url http://localhost:8000/webhooks --webhook-secret $RAZORPAY_WEBHOOK_SECRET \
  --webhook-repeat 3 --webhook-order random \
  --verify-cmd "psql -tAc 'select count(*) from wallet_credits'" --ci
```

```
Side-effect verification (payment.captured)
  deliveries sent: 3000   unique events acked: 1000   counter: 0 -> 3000 (change 3000)
  FAIL: duplicate processing: payment.captured: 1000 unique events caused 3000 side effects; handler is not idempotent
```

No gateway account needed — webhooks are generated locally and signed exactly like the real provider's (including Razorpay's `X-Razorpay-Event-Id`, the key you should dedupe on).

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
carbon report <run_id>

# Check the installed version
carbon --version
```

No payment gateway account needed. Carbon simulates the payment lifecycle locally and sends provider-format signed webhooks. Without `--webhook-url` nothing of yours is tested: the report says so, and the simulator counts it shows are marked `info` (they never pass or fail a run).

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

Use `--set` multiple times for multiple parameters. Unknown keys are ignored with a warning. Entity counts, rates and validation thresholds all follow the parameters (e.g. `dispute-spike` creates `baseline_orders * dispute_rate` disputes).

---

## HTML Reports

Export a shareable HTML report after any run:

```bash
carbon report <run_id> --format html   # --run-id <run_id> also works
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

Every repeat of an event carries the same event id (`X-Razorpay-Event-Id` for Razorpay; the payload `id` for Stripe/Juspay), just like a real provider retry.

A non-idempotent handler still answers `200`, so add `--verify-cmd` to check the actual side effect. It takes any shell command that prints a number — a SQL count, a `curl` to an internal stats endpoint, a Redis `GET`:

```bash
carbon run dispute-spike --provider razorpay --webhook-url http://localhost:8000/webhooks \
  --webhook-repeat 5 --verify-cmd "psql -tAc 'select count(*) from wallet_credits'" \
  --verify-event payment.captured
```

Carbon Layer runs the command before and after delivery and compares the change with the number of unique events your endpoint acknowledged (2xx):

| Result | Meaning |
|---|---|
| `PASS` | each event processed exactly once |
| `FAIL: duplicate processing` | retries were processed as new events (double credit / double fulfilment) |
| `FAIL: lost events` | events were acknowledged with 2xx but never processed |

With `--ci`, a failed verification exits with code 1. The command runs right after the last delivery, so if your handler processes events asynchronously (queue/worker), point it at something that reflects completed work or add a wait (`sleep 5 && psql ...`).

### Webhook plus client-side verify (Razorpay)

Deduping on `X-Razorpay-Event-Id` only covers webhook retries. Many apps also credit the user in their client-side "verify payment" endpoint (the one Razorpay Checkout's `handler` posts `razorpay_order_id`, `razorpay_payment_id` and `razorpay_signature` to). Those are two different requests for the same payment, so an event-id check lets both through.

`--client-verify-url` sends a correctly signed Checkout-style verify request for every captured payment before the webhooks, and the check then counts **one side effect per payment** across both paths:

```bash
carbon run flash-sale --provider razorpay --webhook-url http://localhost:8000/webhooks \
  --webhook-secret $WEBHOOK_SECRET --webhook-repeat 2 \
  --client-verify-url http://localhost:8000/payments/verify --client-verify-secret $RAZORPAY_KEY_SECRET \
  --verify-cmd "psql -tAc 'select count(*) from wallet_credits'"
```

```
deliveries sent: 40   client verify calls: 20   unique payments acked: 20   counter: 0 -> 40 (change 40)
FAIL: duplicate processing: payment.captured: 20 unique payments caused 40 side effects; handler is not idempotent
(the webhook and the client verify call both apply the effect; key it on the payment id)
```

The fix is a unique key on the effect itself (one credit row per `payment_id`), which covers both paths and redelivery. Use `--client-verify-format form` if your endpoint expects form fields instead of JSON.

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

With `missing`, `corrupted` or `wrong_secret`, your handler must reject every webhook with 400, 401 or 403. Any 2xx response is a failure: the report shows `rejects_bad_signatures` as failed ("handler accepted N unsigned webhooks"), the JSON output has `"passed": false`, and `--ci` exits 1. Only the signature header is removed or corrupted; timestamp and version headers (Cashfree, Stripe's `t=`) are left intact so the handler fails on the signature check itself.

If more than 90% of validly signed webhooks get 401/403, Carbon prints a hint that your `--webhook-secret` probably differs from the handler's (the default is `carbon`). If nothing is listening on `--webhook-url`, it says so and exits 3.

---

## Webhook Replay

Replay stored webhook payloads from any previous run. Useful for regression testing after code changes:

```bash
carbon replay <run_id> --webhook-url http://localhost:8000/webhooks
```

---

## CI/CD Integration

Use `--ci` to exit 1 when a graded check fails: a webhook returned 5xx or got no response, a bad signature was accepted with 2xx (see `--webhook-signature`), or `--verify-cmd` found duplicate or lost processing. Exit code 2 means bad usage (e.g. an unknown `--provider`), 3 means nothing was listening on `--webhook-url`.

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

Stdout is exactly one JSON document (all other output goes to stderr), so `| jq` works. It has the run status, `provider`, `webhook_format`, `adapter` (`simulated` unless real Stripe/Cashfree keys were used), findings (simulator counts are flagged `"informational": true`), webhook delivery stats, hints, and overall pass/fail.

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
| Hyperswitch | `X-Webhook-Signature-512` | HMAC-SHA512 | `--provider hyperswitch` |

### Razorpay

```bash
carbon run dispute-spike \
  --provider razorpay \
  --webhook-url https://your-app.com/webhooks \
  --webhook-secret your_webhook_secret
```

No Razorpay keys needed. Razorpay's API can't create payments or disputes server-side, so payments are always simulated locally; if `RAZORPAY_API_KEY`/`RAZORPAY_API_SECRET` are set, Carbon says once that it ignores them.

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
  --webhook-url https://your-app.com/webhooks \
  --webhook-secret your_api_key
```

No Juspay credentials needed: Juspay payments can't be created or captured server-side, so they are always simulated locally (Juspay keys, if set, are ignored).

### Hyperswitch

Testing a merchant integration with [Hyperswitch](https://github.com/juspay/hyperswitch) (Juspay's open-source payments orchestrator)? Carbon Layer sends Hyperswitch-format outgoing webhooks (`payment_succeeded`, `payment_failed`, `refund_succeeded`, `dispute_opened`, …) with the `{event_id, event_type, content: {type, object}}` envelope, signed with your **payment response hash key** exactly like Hyperswitch does (hex HMAC-SHA512 of the raw body in `X-Webhook-Signature-512`):

```bash
carbon run refund-storm --provider hyperswitch \
  --webhook-url http://localhost:8000/webhooks/hyperswitch \
  --webhook-secret $PAYMENT_RESPONSE_HASH_KEY \
  --webhook-repeat 2 --webhook-order random \
  --verify-cmd "psql -tAc 'select count(*) from fulfilments'" --verify-event payment_succeeded
```

No Hyperswitch deployment needed — the payment flow is simulated locally. Repeats of an event share the same `event_id`, so this also tests your dedupe.

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
