# Changelog

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
