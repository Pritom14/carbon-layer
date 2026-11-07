# Changelog

## 0.14.0

AI agents that order and pay through MCP tools.

### Added

- **`carbon agent`.** Runs a local test shop as an MCP server (Streamable HTTP) whose tools follow the payment flow in Swiggy MCP's public builder docs: `get_addresses`, `get_food_cart`, `get_payment_options`, `place_food_order`, `check_payment_status`, `confirm_order`, `get_food_orders`, `track_food_order`. `--agent-cmd` runs your agent with `CARBON_MCP_URL` and `CARBON_AGENT_TASK`; `--serve` waits while you drive the shop from any MCP client. The agent is graded from the shop's ledger of orders and payments.
- Scenarios: `lost-order-response` (the order is created and the response is lost; a blind retry places it twice), `unconfirmed-payment` (the UPI payment succeeds and the order needs `confirm_order`), `confirm-error` (the first `confirm_order` answers HTTP 502), or `all`. Failures name the fix from Swiggy's ship-to-production guide. Warnings for polling faster than `pollingIntervalInMs` and for never checking `get_food_orders` after a lost response.
- `examples/agents/`: a retry-everything agent that fails all three scenarios and a careful agent that passes them.

## 0.13.0

A failed first delivery: the idempotency fix that loses payments.

### Added

- **`--fault-cmd` and `--recover-cmd`.** `--fault-cmd` runs before the first delivery of every event and should break the write your handler makes (revoke INSERT on the credits table, stop the wallet database); `--recover-cmd` runs right after it. Carbon then retries only what was not acknowledged, as a gateway does, and `--verify-cmd` checks every acknowledged event was applied once. A handler that records the event id as processed and applies the credit in separate writes (two databases or two transactions) fails with "marked the event processed before the write failed, so every retry was skipped" and the fix (one transaction, or reconcile by payment id). A handler that answers 200 after a failed write fails with "swallowed the error". If the counter moves while the fault is on, the run fails with "the fault had no effect". `--recover-cmd` always runs, even when `--fault-cmd` or a delivery fails. Raised in a dev.to comment by Karolis.
- JSON output: `verification.fault` (first deliveries, how many returned 2xx, the counter after recovery) and `webhooks.during_fault`.

### Changed

- Deliveries made while `--fault-cmd` is active are left out of the graded webhook checks, hints and the retry outlook: failing them is what the handler should do.

## 0.12.0

Failure modes that return 200 and still lose money, and what the gateway does when your endpoint keeps failing.

### Added

- **Gateway retry outlook (RETRY-03).** Every run with `--webhook-url` projects what the gateway would do with the failures it saw: retry, give up on events, or disable the endpoint ("Razorpay would disable this endpoint after 24 hours"). A delivery counts as failed on a non-2xx, no response, or a 2xx slower than the gateway's published timeout (Razorpay: 5 s). Persistent failures (an event type failing on every delivery, or one event on 3 or more deliveries) fail `--ci` through the new `retry_outlook` check. Each gateway's retry and disable policy lives in one table (`carbon/webhook/retry_policy.py`) with the source URL and quoted sentence; numbers a gateway does not publish (Stripe's disable threshold and timeout, Cashfree's and Hyperswitch's timeouts) are labelled unconfirmed. JSON output has `retry_outlook` and `webhooks.slow`.
- **`late-failure` scenario (FM-04).** Captures every payment, then sends a later `payment.failed` for the same payment; for 25% of payments the two arrive the other way round. With `--verify-cmd` counting orders that are not paid, a handler that lets the last event win fails. Based on the bug in Lemmecode-com/smart-college-mern#194.
- **`double-payment` scenario (FM-21).** Two captured payments (different payment ids) for one order. The checker counts orders credited more than once.
- **`--webhook-concurrency N` (FM-21).** Sends every webhook N times at the same instant, which exposes check-then-insert races that sequential retries never trigger. The failure message names the fix (a unique constraint with `ON CONFLICT DO NOTHING`).
- **`upi-timeout` rewritten (FM-27).** Timed-out UPI payments now get `payment.failed` (Razorpay error reason `payment_timed_out`, method `upi`), then a late `payment.captured` for the same payment after `late_delay_s`. The checker flags cancelled orders whose payment was captured and not refunded.
- **`--verify-expect acked|unchanged`.** `unchanged` treats the `--verify-cmd` number as a count of records in a bad state that must not grow. The three scenarios above default to it and print a `Fix:` line on failure.
- Scenario building blocks: `late_events` (another event for the same payment, in a later delivery batch, or before the original with `reorder_rate`), `duplicate_payments`, `for_each: failed_payments`, `method` and `failure_reason` on `create_payments`, a `late_delay_s` parameter for the pause between batches, and a `verify:` block for `--verify-cmd` defaults.

### Changed

- `--webhook-repeat` delivers repeats as later retries: each round starts after every delivery of the previous round got its response, so two copies of one event never overlap. Use `--webhook-concurrency` for overlapping copies.
- For Razorpay-format webhooks Carbon waits up to 10 s for a response, so a slow 2xx is reported as "too slow" rather than a bare timeout; `webhook_delivery` still grades anything past 5 s as failed. The delivery summary gets a `2xx > 5s` column when there are any.
- `--webhook-repeat` and `--webhook-concurrency` below 1 exit with code 2.
