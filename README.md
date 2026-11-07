# Carbon Layer

[![PyPI](https://img.shields.io/pypi/v/carbon-layer)](https://pypi.org/project/carbon-layer/)
[![CI](https://github.com/Pritom14/carbon-layer/actions/workflows/ci.yml/badge.svg)](https://github.com/Pritom14/carbon-layer/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/carbon-layer)](https://pypi.org/project/carbon-layer/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

Chaos engineering for payment flows.

Every company processing payments tests the happy path (payment succeeds, order fulfilled) and ships. What breaks in production is everything else: dispute spikes your system doesn't respond to, refund storms that break reconciliation, gateway errors that leave orders stuck, webhook sequences your handlers were never tested against.

Carbon Layer lets you simulate these failure modes against your own integration before your customers encounter them. Run a scenario, point it at your webhook endpoint, and see exactly what your system handles and what it doesn't.

### Does your handler credit a payment twice?

Razorpay, Stripe and Cashfree all deliver webhooks *at least once*, so the same `payment.captured` can arrive more than once. A handler that doesn't dedupe returns `200` every time, so status codes alone will never show the bug. Carbon Layer checks the side effect itself:

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

No gateway account needed: webhooks are generated locally and signed exactly like the real provider's (including Razorpay's `X-Razorpay-Event-Id`, the key you should dedupe on).

---

## Installation

### Option A: Quick start (no database needed)

```bash
pip install carbon-layer
```

Works out of the box. Carbon Layer uses SQLite by default; data is stored in `~/.carbon/carbon.db`. Nothing else to install or configure.

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

The real value of Carbon Layer is testing your webhook handlers. Point it at your endpoint and it fires provider-specific webhook events (`payment.captured`, `payment_intent.succeeded`, `PAYMENT_SUCCESS_WEBHOOK`, `ORDER_SUCCEEDED`, and more) after the scenario runs. Payloads are signed exactly like real webhooks from each provider (Razorpay, Stripe, Cashfree, Juspay).

```bash
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks
```

The report shows how your endpoint responded for each event type: 2xx, 4xx, 5xx, or timeout. No payment gateway account required.

---

## Scenarios

| Scenario | What it tests |
|----------|---------------|
| `dispute-spike` | 15% dispute rate: does your system respond and submit evidence? |
| `payment-decline-spike` | 30% payment failure rate: does your retry and order state logic hold? |
| `refund-storm` | Mass refunds on captured payments: does reconciliation break? |
| `flash-sale` | High order and payment volume: does throughput hold? |
| `gateway-error-burst` | Intermittent gateway errors: are orders left in inconsistent state? |
| `min-amount` | Minimum paise transactions: are edge-case amounts handled correctly? |
| `max-amount` | Large-value transactions: are limits and approvals handled correctly? |
| `upi-timeout` | UPI payment times out, the order is cancelled, then `payment.captured` arrives late: is the money refunded or the order reinstated? |
| `vpa-not-found` | Invalid UPI VPA failures: does your handler distinguish VPA errors from general failures? |
| `mandate-rejection` | UPI autopay mandate rejections: does your handler notify the customer? |
| `settlement-delay` | Refunds on captured-but-unsettled payments: does reconciliation handle it? |
| `late-failure` | `payment.failed` arrives after `payment.captured` for the same payment (and the other way round): does a paid order stay paid? |
| `double-payment` | Two captured payments for one order (double click, client retry): is the order credited once? |

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

Writes `carbon_report_<run_id>.html` to the current directory. Self-contained, no external dependencies, safe to share with your team or attach to an incident report.

---

## Webhook Resilience Testing

Test how your webhook handler behaves under real-world failure conditions.

### Idempotency (duplicate webhooks)

Fire each webhook multiple times to test whether your handler processes duplicates:

```bash
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks --webhook-repeat 5
```

Every repeat of an event carries the same event id (`X-Razorpay-Event-Id` for Razorpay; the payload `id` for Stripe/Juspay), just like a real provider retry. Repeats go out as later retries: each round starts once every delivery of the previous round got its response, so two copies of one event never overlap. To send copies at the same instant, use `--webhook-concurrency` (below).

A non-idempotent handler still answers `200`, so add `--verify-cmd` to check the actual side effect. It takes any shell command that prints a number: a SQL count, a `curl` to an internal stats endpoint, a Redis `GET`:

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

### Concurrent duplicates (check-then-insert races)

At-least-once delivery means two copies of one event can land at the same moment: a gateway retry that overlaps a slow first attempt, or two workers pulling the same message. A handler that asks "already processed?" and then inserts passes every sequential retry and still credits twice when copies arrive together, because both pass the check before either has saved. `--webhook-concurrency N` sends every webhook N times at the same instant:

```bash
carbon run flash-sale --provider razorpay --set baseline_orders=20 \
  --webhook-url http://localhost:8000/webhooks --webhook-secret $RAZORPAY_WEBHOOK_SECRET \
  --webhook-concurrency 5 --verify-cmd "psql -tAc 'select count(*) from wallet_credits'" --ci
```

Real run against a handler that checks for an existing credit, then inserts one (the same handler passes with `--webhook-repeat 5`):

```
Side-effect verification (payment.captured)
  deliveries sent: 100   unique events acked: 20   counter: 0 -> 100 (change 100)
  FAIL: duplicate processing: payment.captured: 20 unique events caused 100 side effects; handler is not idempotent (5 copies arrived at the same moment and each passed an 'already processed?' check before any was saved: a check-then-insert race. Put a unique constraint on the event or payment id and insert with ON CONFLICT DO NOTHING)
```

The fix is a unique constraint on the effect (one credit per payment or event id) with `INSERT ... ON CONFLICT DO NOTHING`, so the database turns away the second copy however the requests interleave. A lock in application code only covers one process. `--webhook-concurrency` combines with `--webhook-repeat` (N copies at once, in each retry round).

### A failed first delivery (`--fault-cmd`)

The usual idempotency fix records the event id as processed and applies the credit. If those are two separate writes (the event table and the wallet in different databases, or two transactions), a failure between them is worse than a duplicate: the first attempt records the event and then fails, every retry sees "already processed" and returns 200, and the customer paid while the wallet never moved. A normal run never fails half way, so the bug stays hidden.

`--fault-cmd` breaks the write your handler makes for the first delivery of every event, `--recover-cmd` undoes it, and then carbon retries what was not acknowledged, the way the gateway would (a 2xx is never retried). `--verify-cmd` then checks that every acknowledged event was applied exactly once:

```bash
carbon run flash-sale --provider razorpay \
  --webhook-url http://localhost:8000/webhooks --webhook-secret $RAZORPAY_WEBHOOK_SECRET \
  --fault-cmd "psql -c 'revoke insert on wallet_credits from app'" \
  --recover-cmd "psql -c 'grant insert on wallet_credits to app'" \
  --verify-cmd "psql -tAc 'select count(*) from wallet_credits'" --ci
```

Real run against a handler that keeps processed event ids in one SQLite database and credits in another:

```
Fault mode: 50 first deliveries with the write broken (0 returned 2xx), 50 retries after recovery
Side-effect verification (payment.captured)
  deliveries sent: 100   unique events acked: 50   counter: 0 -> 0 (change 0)
  with the fault: 50 first deliveries (0 returned 2xx), counter 0 -> 0; retries after recovery took it to 0
  FAIL: lost events: payment.captured: 50 unique events acknowledged (2xx) but only 0 side effects after a failed first delivery. The first delivery ran with the write broken and the handler marked the event processed before the write failed, so every retry was skipped. Record the event id and apply the effect in one database transaction, or reconcile missing effects by payment id
```

The same handler with the event id and the credit in one transaction passes: the first attempt fails cleanly (nothing recorded), and the retry credits once. Other things the run reports:

- A handler that catches the failed write and returns 200 anyway fails too, with "the handler swallowed the error, so a gateway would never retry them".
- If the counter rises while the fault is on, `--fault-cmd` did not break anything and the run fails with "the fault had no effect", so a broken fault never passes silently.
- `--recover-cmd` always runs after the first delivery, even if `--fault-cmd` or a delivery failed. The failed first deliveries are not graded as 5xx failures and do not count towards the retry outlook.

Any command that makes your write fail fast works: revoking INSERT, stopping the database that holds the credits (`docker stop` / `docker start`), or a feature flag your handler reads. `--webhook-repeat` sets the attempts (default 3 in fault mode: the first delivery and up to two retries). Raised by Karolis in a comment on the [dev.to post](https://dev.to/pritom14/does-your-razorpay-webhook-credit-the-same-payment-twice-50-second-demo-23na).

### Out-of-order delivery

Gateways don't guarantee webhook order. Test with randomized or reversed delivery:

```bash
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks --webhook-order random
```

Options: `sequence` (default), `reverse`, `random`.

### State checks (`--verify-expect unchanged`)

By default `--verify-cmd` counts a side effect that must grow by exactly one per unique event your endpoint acknowledged (`--verify-expect acked`). Some bugs leave a bad state instead: an order credited twice, a paid order marked failed, a cancelled order whose money was kept. With `--verify-expect unchanged` the command counts records in a bad state, and the run fails if that count grows. The `late-failure`, `double-payment` and `upi-timeout` scenarios use `unchanged` by default, and print what to fix:

```
State check: unpaid orders (must not grow)
  deliveries sent: 40   checker: 0 -> 15
  FAIL: 15 new unpaid orders
  Fix: ignore payment.failed for a payment already captured, and let payment.captured win when it arrives after a failure: an order only moves forward, never back
```

### Late `payment.failed` after `payment.captured` (`late-failure`)

A `payment.failed` for a payment you already captured can still arrive: a retried event from an earlier failed attempt, or a delayed one. A handler where the last event wins moves the order from paid back to failed, and the customer who paid gets cancelled. This is a real bug report: in [Lemmecode-com/smart-college-mern#194](https://github.com/Lemmecode-com/smart-college-mern/issues/194) the audit found "a late or retried `payment.failed` could revert a PAID installment to PENDING".

`late-failure` captures every payment, then sends a `payment.failed` for the same payment id in a later batch (`late_delay_s`, default 0.5 s). For a quarter of the payments (`reorder_rate`) the order is reversed: `payment.failed` first, `payment.captured` after, which catches the opposite bug, a handler that ignores a capture once it has seen a failure.

```bash
carbon run late-failure --provider razorpay \
  --webhook-url http://localhost:8000/webhooks --webhook-secret $RAZORPAY_WEBHOOK_SECRET \
  --verify-cmd "psql -tAc \"select count(*) from orders where status <> 'paid'\"" --ci
```

Real run against a last-event-wins handler: `checker: 0 -> 15`, `FAIL: 15 new unpaid orders` (the 15 whose failure came after the capture; the 5 reordered ones ended paid). The fixed handler only moves an order forward and passes with `0 -> 0`.

### Two payments for one order (`double-payment`)

A double click, or a client that retries checkout after a timeout, creates two payments for one order. Each has its own payment id and its own `payment.captured` event, so deduping on the event id or the payment id lets both through and the order is credited twice. `double-payment` gives half the orders (`double_pay_rate`) a second captured payment:

```bash
carbon run double-payment --provider razorpay \
  --webhook-url http://localhost:8000/webhooks --webhook-secret $RAZORPAY_WEBHOOK_SECRET \
  --verify-cmd "psql -tAc 'select count(*) from (select order_id from credits group by order_id having count(*) > 1) t'" --ci
```

The checker counts orders credited more than once and must stay at its starting value. The fix is a unique constraint on the order's credit, plus a refund of the second payment.

### UPI timeouts that succeed later (`upi-timeout`)

A UPI payment can time out for the customer and still succeed hours later. Paytm documents that it "polls the transaction status from bank for next 72 hours" and sends the final status when it arrives ([late notification](https://paytmpayments.com/docs/late-notification)). Razorpay documents late authorisation: a `payment.authorized` webhook arrives when a late payment is authorised, and uncaptured ones are refunded automatically ([late authorisation](https://razorpay.com/docs/payments/payments/late-authorisation/handle/)). If your account captures automatically, the capture and its `payment.captured` follow. By then many shops have cancelled the order and released the stock.

`upi-timeout` times out 30% of UPI payments: Carbon sends `payment.failed` with Razorpay's UPI error reason `payment_timed_out`, your shop cancels the order (on that event, or with its own pending-order timeout), and after `late_delay_s` (default 1 s here, hours in production) `payment.captured` arrives for the same payment. A handler that answers 200 and ignores a capture for a cancelled order keeps the customer's money with nothing delivered.

```bash
carbon run upi-timeout --provider razorpay --set baseline_orders=20 \
  --webhook-url http://localhost:8000/webhooks --webhook-secret $RAZORPAY_WEBHOOK_SECRET \
  --verify-cmd "psql -tAc \"select count(*) from orders o join payments p on p.order_id = o.id where o.status = 'cancelled' and p.status = 'captured' and p.id not in (select payment_id from refunds)\"" --ci
```

Real run against a handler that ignores captures for cancelled orders: `checker: 0 -> 6`, `FAIL: 6 new cancelled orders with a captured, unrefunded payment`. Refund the payment or reinstate the order. If your shop cancels on a timer, raise `--set late_delay_s=` above it.

### Signature verification

Test whether your handler actually validates webhook signatures:

```bash
# Missing signatures: should your handler reject these?
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

## AI agents that order and pay (`carbon agent`)

An AI agent that places orders through MCP tools has the same problems as a webhook handler, from the other side. Its order call times out after the order was created; a generic retry places it again. A UPI payment succeeds, and the agent stops before `confirm_order`, so the customer paid and nothing was ordered.

`carbon agent` runs a local test shop whose tools follow the payment flow in [Swiggy MCP's builder docs](https://mcp.swiggy.com/builders/docs/build/ship-to-production/) (`get_food_cart`, `place_food_order`, `check_payment_status`, `confirm_order`, `get_food_orders`, ...), so an agent written for it can be pointed here unchanged. Each scenario injects one fault, then carbon grades the agent from the shop's own ledger of orders and payments, not from what the agent says it did. Nothing touches a real store or real money. (carbon-layer is not affiliated with Swiggy.)

```bash
carbon agent all --agent-cmd "python my_agent.py"
```

Your agent gets `CARBON_MCP_URL` (an MCP server, Streamable HTTP) and `CARBON_AGENT_TASK` (what to ask an LLM agent to do), and should place one order.

| Scenario | Fault | Fails when |
|---|---|---|
| `lost-order-response` | `place_food_order` creates the order, then the connection drops before the response | the agent places the cart again: 2 orders for one cart |
| `unconfirmed-payment` | the UPI payment succeeds with `confirmed: false` | the agent never calls `confirm_order`: paid, order never placed |
| `confirm-error` | the first `confirm_order` answers HTTP 502 | the agent gives up, or places a new order instead of retrying `confirm_order` (it is idempotent) |

```
carbon agent  lost-order-response
place_food_order creates the order, then the response is lost.
calls   get_addresses 1, get_food_cart 1, place_food_order 2
orders  2  (CONFIRMED, CONFIRMED)

FAIL  2 orders placed for one cart. The first place_food_order response was lost
      after the order was created, and the agent placed it again without
      checking get_food_orders.
Fix   Never blind-retry place_food_order. After a lost or failed response, wait
      2-5 s and call get_food_orders; retry only if the order is not there.
```

Warnings (they do not fail the run): polling `check_payment_status` faster than `pollingIntervalInMs`, and never checking `get_food_orders` after a lost response. Exit code 1 if any scenario fails or the agent places no order; `--output json` prints every tool call and order.

To drive the shop from an MCP client yourself (Claude, Cursor, MCP Inspector), use `--serve`: carbon prints the URL (default port 8765) and the task, and grades the run when you press Enter.

```bash
carbon agent lost-order-response --serve
```

Two example agents are in [`examples/agents/`](examples/agents/): `retry_agent.py` retries every failed tool call and stops when the payment succeeds (it fails all three), and `careful_agent.py` follows Swiggy's ship-to-production guidance (it passes all three).

## Gateway retry outlook

A handler that times out or errors doesn't just lose one event. The gateway retries it, then gives up, and some gateways switch the endpoint off, after which every later event goes undelivered until someone re-enables it. Every run with `--webhook-url` ends with a projection of what the gateway would do with the failures it saw:

```
Gateway retry outlook (Razorpay)
  WARNING: Razorpay would disable this endpoint after 24 hours
  failed deliveries: 20 of 25 (80%): 20 too slow
  20 answered 2xx after Razorpay's 5 s timeout (slowest 6.1 s); Razorpay counts them as failed and retries
  failed on every delivery: payment.captured (20 of 20)
  Razorpay retries a failed event with exponential backoff for 24 hours, and disables the webhook if it keeps failing for 24 hours, until you re-enable it in the Dashboard.
  Once it is disabled, every later event for this endpoint goes undelivered.
  Return 2xx first and do the slow work (emails, invoices, API calls) in a background job.
  Source: https://razorpay.com/docs/webhooks/best-practices/
```

That is a real run of `carbon run refund-storm --provider razorpay --set baseline_orders=20 --webhook-url ... --ci` against a handler that emails the invoice before answering `payment.captured` (about 6 s). It answers 200 every time, and `--ci` still exits 1. The same handler with the email moved to a background worker gets `OK: Razorpay would keep this endpoint enabled`.

How it is projected:

- A delivery fails when it gets a non-2xx (3xx, 4xx or 5xx), no response, or a 2xx slower than the gateway's published timeout. For Razorpay Carbon waits 10 s, so a slow 2xx shows up as "too slow" instead of a bare timeout.
- A failure is persistent when an event type fails on every delivery, or one event fails on 3 or more deliveries in a row (try `--webhook-repeat 3`). The gateway's retries would fail the same way, so it gives up on those events and, where documented, disables the endpoint. `--ci` fails (check `retry_outlook`).
- Scattered failures are left to the retries. They still fail `webhook_delivery` when they are 5xx or timeouts.

Each gateway's policy is a data table in [`retry_policy.py`](src/carbon/webhook/retry_policy.py), with the sentence each number comes from:

| Gateway | Response timeout | Retries | Auto-disable | Source |
|---|---|---|---|---|
| Razorpay | 5 s | exponential backoff for 24 hours (intervals not published) | after 24 hours of failing | [best practices](https://razorpay.com/docs/webhooks/best-practices/) |
| Stripe | not published | exponential backoff for up to 3 days in live mode; 3 times over a few hours in a sandbox | after "several days" (number not published: Carbon projects 3 days and says it is unconfirmed) | [webhooks](https://docs.stripe.com/webhooks#automatic-retries), [support](https://support.stripe.com/questions/why-is-stripe-trying-to-reach-my-webhook-endpoints) |
| Cashfree | not published | 3 retries at 2, 10 and 30 minute intervals (default policy) | not documented for payment webhooks (Payouts V2 and Cashgram: after more than 5 consecutive failures) | [reliability](https://www.cashfree.com/docs/api-reference/webhooks/reliability-and-operations) |
| Hyperswitch | not published | 16 retries over about 24 hours | not documented | [webhooks](https://docs.hyperswitch.io/hyperswitch-cloud/webhooks) |
| Juspay | not published | not published | not published | no projection |

Where a gateway publishes no timeout, Carbon keeps its own 5 s client timeout. `--output json` includes the projection as `retry_outlook`.

---

## Security checks

`carbon security` attacks your webhook endpoint directly, with no scenario run, and prints a 0-100 scorecard. Each check sends a small batch of requests and grades your handler's status codes:

| Check | Sends | Expected | Severity |
|---|---|---|---|
| Valid signature accepted | a correctly signed webhook | 2xx | high |
| Raw body (re-serialized JSON) | the same JSON pretty-printed, with keys reordered, and with `\u00eb` escapes, each signed over the exact bytes sent | 2xx for all | high (correctness) |
| Unsigned / corrupted signature / wrong secret | signature header removed, corrupted, or made with another secret | 4xx | critical |
| Empty/default secrets | signatures made with `''`, `secret`, `test`, `whsec_test` | 4xx | critical |
| Tampered amount | body with amounts x100, carrying the valid signature of the original body | 4xx | critical |
| Stale / future timestamp | Stripe `t=` or Cashfree `x-webhook-timestamp` 10 minutes old / ahead, validly signed (n/a for Razorpay, Juspay, Hyperswitch) | 4xx | high |
| Client verify: forged signature, swapped ids, other order's payment | with `--client-verify-url` (Razorpay Checkout verify endpoint) | 4xx | critical |

```bash
carbon security --provider razorpay \
  --webhook-url http://localhost:8000/webhooks --webhook-secret $RAZORPAY_WEBHOOK_SECRET \
  --client-verify-url http://localhost:8000/payments/verify --ci
```

Sample scorecard from a real run against a handler that verifies the HMAC over `json.dumps(json.loads(body))` (the Python twin of `JSON.stringify(req.body)`):

```
Webhook security: razorpay -> http://127.0.0.1:8002/webhooks
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Check                            ┃ Severity ┃ Want ┃ Got                ┃ Result                           ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Valid signature accepted         │ high     │ 2xx  │ 200                │ PASS accepted all 1              │
│ Raw body (re-serialized JSON)    │ high     │ 2xx  │ 401, 200, 401      │ FAIL REJECTS VALID WEBHOOKS sent │
│                                  │          │      │                    │ as: pretty-printed (whitespace), │
│                                  │          │      │                    │ unicode escapes (\u00eb). The    │
│                                  │          │      │                    │ signature is checked against     │
│                                  │          │      │                    │ re-serialized JSON, not the raw  │
│                                  │          │      │                    │ body                             │
│ Unsigned                         │ critical │ 4xx  │ 401                │ PASS rejected all 1              │
│ Corrupted signature              │ critical │ 4xx  │ 401                │ PASS rejected all 1              │
│ Wrong secret                     │ critical │ 4xx  │ 401                │ PASS rejected all 1              │
│ Empty/default secrets            │ critical │ 4xx  │ 401, 401, 401, 401 │ PASS rejected all 4              │
│ Tampered amount                  │ critical │ 4xx  │ 401                │ PASS rejected all 1              │
│ Client verify: valid             │ high     │ 2xx  │ 200                │ PASS accepted all 1              │
│ Client verify: forged signature  │ critical │ 4xx  │ 401, 401           │ PASS rejected all 2              │
│ Client verify: swapped ids       │ critical │ 4xx  │ 401                │ PASS rejected all 1              │
│ Client verify: other order's     │ critical │ 4xx  │ 401                │ PASS rejected all 1              │
│ payment                          │          │      │                    │                                  │
│ Stale timestamp                  │ high     │ 4xx  │ -                  │ N/A no signed timestamp          │
│ Future timestamp                 │ high     │ 4xx  │ -                  │ N/A no signed timestamp          │
└──────────────────────────────────┴──────────┴──────┴────────────────────┴──────────────────────────────────┘

How to fix
  raw_body: Compute the HMAC over the exact request bytes (Flask request.get_data(), Django request.body,
Express express.raw()), never over json.dumps/JSON.stringify of the parsed body: re-serializing changes
whitespace, key order and escapes.

Security score: 95/100
```

The raw-body check catches handlers that rebuild the JSON before verifying. That isn't a security hole, but the handler **rejects real webhooks** whenever the gateway's bytes differ from your re-serialization (whitespace, key order, escaped non-ASCII text such as names or notes), and the gateway then retries and eventually disables the endpoint. The fix is to HMAC the raw request body (`request.get_data()` in Flask, `request.body` in Django, `express.raw()` in Express) and parse it only after verifying. Stripe and Cashfree both document this; Razorpay warns "Do Not Parse or Cast the Webhook Request Body".

Scoring: critical checks weigh 10, high checks 5; the score is the weighted share passed, and any critical failure caps it at 40, because one accepted forgery lets anyone fake a payment. A check is `n/a` when the provider has no such mechanism, and `inconclusive` when your handler accepted no valid webhook at all (usually a wrong `--webhook-secret`), since rejecting forgeries then proves nothing. A rejection must be a 4xx; 5xx or no response fails the check.

`--ci` exits 1 when any check fails (rejecting valid webhooks loses payments; accepting stale timestamps allows replays) or a critical check can't be graded. Use `--fail-on critical` to fail only on critical checks. `--output json` prints one JSON document on stdout (score, every check with expected outcome, observed status codes, result and fix), with everything else on stderr. Exit 3 means nothing was listening on `--webhook-url`.

---

## Webhook Replay

Replay stored webhook payloads from any previous run. Useful for regression testing after code changes:

```bash
carbon replay <run_id> --webhook-url http://localhost:8000/webhooks
```

---

## CI/CD Integration

Use `--ci` to exit 1 when a graded check fails: a webhook returned 5xx, got no response or answered after the gateway's published timeout, the [retry outlook](#gateway-retry-outlook) projects that the gateway would disable the endpoint or give up on events, a bad signature was accepted with 2xx (see `--webhook-signature`), or `--verify-cmd` found duplicate or lost processing or a new bad state. Exit code 2 means bad usage (e.g. an unknown `--provider`), 3 means nothing was listening on `--webhook-url`.

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

Stdout is exactly one JSON document (all other output goes to stderr), so `| jq` works. It has the run status, `provider`, `webhook_format`, `adapter` (`simulated` unless real Stripe/Cashfree keys were used), findings (simulator counts are flagged `"informational": true`), webhook delivery stats (including `slow`: 2xx after the gateway's timeout), `verification` (with `mode`: `acked` or `unchanged`), `retry_outlook`, hints, and overall pass/fail.

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

No Hyperswitch deployment needed: the payment flow is simulated locally. Repeats of an event share the same `event_id`, so this also tests your dedupe.

### Mock (no credentials needed)

```bash
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks
```

Mock mode simulates the full payment lifecycle locally. Use this if you don't have test credentials; all 13 scenarios work out of the box.

---

## Carbon Layer Pro (coming soon)

The open-source CLI covers 13 scenarios, 5 payment gateways, and webhook resilience testing. We're building a hosted Pro tier for teams that need more:

- **Scheduled runs:** run scenarios on a cron, get notified when your handlers regress
- **PDF reports:** export compliance-ready reports for audits and stakeholders
- **Compliance dashboards:** track webhook handler reliability over time
- **Pro scenarios:** RBI compliance checks, PCI readiness suite, 10k TPS load tests, multi-gateway failover, settlement reconciliation
- **Team management:** shared runs, role-based access, audit logs

The free CLI stays free and open-source (Apache 2.0). Pro is a separate hosted product.

Join the waitlist: [pritom14.github.io/carbon-layer/waitlist](https://pritom14.github.io/carbon-layer/waitlist)

---

## License

Apache 2.0, see [LICENSE](./LICENSE).
