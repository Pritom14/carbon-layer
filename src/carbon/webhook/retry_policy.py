"""What a gateway would do with your endpoint's failures: retry, give up, or disable it.

A handler that answers 500, 4xx or too slowly doesn't just lose one event: the gateway
retries it, and if the failures keep coming it stops retrying or switches the endpoint off,
after which every later event is undelivered. POLICIES holds each gateway's documented retry
and disable behaviour, with the source and the sentence each number comes from;
project() applies it to the deliveries of a run.

Numbers a gateway does not publish are None, and the output says so.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RetryPolicy:
    gateway: str
    retries: str  # how the gateway retries one failed event
    timeout_s: Optional[float]  # response timeout; a slower 2xx is a failure. None: not published
    disable_after: Optional[str]  # sustained failure after which the endpoint is disabled
    disable_confirmed: bool  # False when disable_after is carbon's estimate, not a published number
    disable_rule: str  # what the docs say about disabling
    source: str
    caveat: str = ""  # what could not be confirmed


# All pages checked on 2026-11-02.
POLICIES: dict[str, RetryPolicy] = {
    # https://razorpay.com/docs/webhooks/best-practices/
    #   "your server accepts the event but fails to respond in 5 seconds ... the session is
    #    marked timeout"
    #   "we retry the delivery in exponential backoff policy for 24 hours"
    #   "If the webhooks continue to fail for 24 hours, the webhook is disabled."
    #   "You need to enable the webhook from the Dashboard after fixing the errors"
    # The individual retry intervals are not published.
    "razorpay": RetryPolicy(
        gateway="Razorpay",
        retries="retries a failed event with exponential backoff for 24 hours",
        timeout_s=5.0,
        disable_after="24 hours",
        disable_confirmed=True,
        disable_rule="disables the webhook if it keeps failing for 24 hours, until you re-enable "
        "it in the Dashboard",
        source="https://razorpay.com/docs/webhooks/best-practices/",
    ),
    # https://docs.stripe.com/webhooks#automatic-retries
    #   "Stripe attempts to deliver events to your destination for up to three days with an
    #    exponential backoff in live mode." Sandbox: "three times over the course of a few hours".
    # https://support.stripe.com/questions/why-is-stripe-trying-to-reach-my-webhook-endpoints
    #   "If a webhook endpoint has been unresponsive or responding with errors for several days
    #    and remains in that state, we stop delivery attempts to that endpoint and disable it"
    # Stripe publishes neither the number of days nor its response timeout. carbon projects
    # 3 days (the live-mode retry window) and labels the number unconfirmed.
    "stripe": RetryPolicy(
        gateway="Stripe",
        retries="retries a failed event with exponential backoff for up to 3 days in live mode "
        "(3 times over a few hours in a sandbox)",
        timeout_s=None,
        disable_after="3 days",
        disable_confirmed=False,
        disable_rule="disables an endpoint that has been unresponsive or responding with errors "
        "for several days",
        source="https://support.stripe.com/questions/why-is-stripe-trying-to-reach-my-webhook-endpoints",
        caveat="Stripe says 'several days' and does not publish the number; 3 days is its "
        "live-mode retry window. Its response timeout is not published either",
    ),
    # https://www.cashfree.com/docs/api-reference/webhooks/reliability-and-operations
    #   Default policy: "Retries up to three times at 2, 10, and 30-minute intervals"
    #   "Responses outside the 2xx range, including 3xx redirects, count as delivery failures."
    # Auto-disable is documented only for Payouts V2 and Cashgram ("If your endpoint fails more
    # than five consecutive times, the webhook endpoint is disabled automatically"), not for
    # payment gateway webhooks. No response timeout is published.
    "cashfree": RetryPolicy(
        gateway="Cashfree",
        retries="retries a failed event 3 times, at 2, 10 and 30 minute intervals (default policy)",
        timeout_s=None,
        disable_after=None,
        disable_confirmed=False,
        disable_rule="does not document disabling payment webhook endpoints (it does for Payouts "
        "V2 and Cashgram, after more than 5 consecutive failures)",
        source="https://www.cashfree.com/docs/api-reference/webhooks/reliability-and-operations",
        caveat="Cashfree publishes no response timeout",
    ),
    # https://docs.hyperswitch.io/hyperswitch-cloud/webhooks
    #   "The scheduler makes 16 retries over roughly 24 hours": 1 minute after the original
    #   attempt, then 5 min x2, 10 min x5, 1 h x5, 6 h x3. "A delivery succeeds when your
    #   endpoint returns a 2xx response." Neither a response timeout nor endpoint disabling
    #   is documented.
    "hyperswitch": RetryPolicy(
        gateway="Hyperswitch",
        retries="retries a failed event 16 times over about 24 hours",
        timeout_s=None,
        disable_after=None,
        disable_confirmed=False,
        disable_rule="does not document disabling webhook endpoints",
        source="https://docs.hyperswitch.io/hyperswitch-cloud/webhooks",
        caveat="Hyperswitch publishes no response timeout",
    ),
    # Juspay: no public retry or disable numbers found, so no projection.
}

# carbon's HTTP timeout when the gateway publishes none (unchanged from earlier versions)
DEFAULT_CLIENT_TIMEOUT_S = 5.0
# Extra wait past a published gateway timeout, so a slow 2xx is seen and reported as too slow
SLOW_GRACE_S = 5.0


def gateway_timeout_s(provider: str) -> Optional[float]:
    policy = POLICIES.get(provider)
    return policy.timeout_s if policy else None


def client_timeout_s(provider: str) -> float:
    """How long carbon waits for a response: past the gateway's timeout, to observe slow 2xx."""
    timeout = gateway_timeout_s(provider)
    return timeout + SLOW_GRACE_S if timeout else DEFAULT_CLIENT_TIMEOUT_S


def failure_reason(d: dict, timeout_s: Optional[float]) -> Optional[str]:
    """Why the gateway would count this delivery as failed, or None if it would count as delivered."""
    sc = d.get("status_code")
    if sc is None:
        return "no response"
    if not 200 <= sc < 300:
        return f"{sc // 100}xx"
    if timeout_s is not None and (d.get("duration_ms") or 0) > timeout_s * 1000:
        return "slow"
    return None


def _fmt_s(seconds: float) -> str:
    return f"{seconds:.1f} s" if seconds % 1 else f"{seconds:.0f} s"


def _fix(reasons: Counter) -> str:
    top = reasons.most_common(1)[0][0]
    if top == "slow":
        return "Return 2xx first and do the slow work (emails, invoices, API calls) in a background job."
    if top == "4xx":
        return (
            "Return 2xx for validly signed events you don't act on; to the gateway a 4xx is a "
            "failed delivery, so it retries."
        )
    if top == "no response":
        return "Answer every webhook within the timeout, even when your own dependencies are down."
    return "Fix the error, or acknowledge with 2xx and retry the work on your side."


def project(deliveries: list[dict], provider: str) -> Optional[dict]:
    """Project the gateway's reaction to the failures seen in deliveries (validly signed webhooks).

    A failure that repeats for an event type on every delivery (or for one event on 3 or more
    deliveries in a row) is persistent: the gateway's retries would fail too, so it would stop retrying and,
    where documented, disable the endpoint. Scattered failures are left to the retries.
    """
    if not deliveries:
        return None
    policy = POLICIES.get(provider)
    timeout = policy.timeout_s if policy else None
    reasons: Counter = Counter()
    by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_event: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    slowest_ms = 0
    for d in deliveries:
        why = failure_reason(d, timeout)
        failed = why is not None
        if failed:
            reasons[why] += 1
        if why == "slow":
            slowest_ms = max(slowest_ms, d.get("duration_ms") or 0)
        t = by_type[d.get("event_type") or "unknown"]
        t[0] += 1
        t[1] += failed
        e = by_event[d.get("provider_event_id") or d.get("payload") or id(d)]
        e[0] += 1
        e[1] += failed

    total = len(deliveries)
    n_failed = sum(reasons.values())
    always_failing = {t: n for t, (n, f) in by_type.items() if f == n}
    # Three failed attempts at one event is a pattern, not bad luck
    every_retry_failed = sum(1 for n, f in by_event.values() if n >= 3 and f == n)
    persistent = bool(always_failing or every_retry_failed)
    name = policy.gateway if policy else provider.capitalize()

    labels = {"slow": "too slow", "no response": "no response"}
    breakdown = ", ".join(f"{n} {labels.get(r, r)}" for r, n in reasons.most_common())
    lines: list[str] = []
    if n_failed:
        lines.append(f"failed deliveries: {n_failed} of {total} ({100 * n_failed / total:.0f}%): {breakdown}")
    if reasons.get("slow"):
        lines.append(
            f"{reasons['slow']} answered 2xx after {name}'s {_fmt_s(timeout)} timeout "
            f"(slowest {slowest_ms / 1000:.1f} s); {name} counts them as failed and retries"
        )
    if always_failing:
        lines.append(
            "failed on every delivery: "
            + ", ".join(f"{t} ({n} of {n})" for t, n in sorted(always_failing.items()))
        )
    if every_retry_failed:
        lines.append(f"{every_retry_failed} events failed on all of their 3 or more deliveries")

    if policy is None:
        verdict = "unknown"
        headline = f"{name} publishes no webhook retry policy, so carbon can't project one"
    elif not n_failed:
        verdict = "ok"
        within = f" within the {_fmt_s(timeout)} timeout" if timeout else ""
        headline = f"{name} would keep this endpoint enabled: every delivery got a 2xx{within}"
    elif persistent and policy.disable_after:
        verdict = "disable"
        about = "" if policy.disable_confirmed else "about "
        headline = f"{name} would disable this endpoint after {about}{policy.disable_after}"
        lines.append(f"{name} {policy.retries}, and {policy.disable_rule}.")
        lines.append("Once it is disabled, every later event for this endpoint goes undelivered.")
    elif persistent:
        verdict = "stops_retrying"
        headline = f"{name} would give up on these events: it {policy.retries}"
        lines.append(f"After that the event is not retried again. {name} {policy.disable_rule}.")
    else:
        verdict = "recoverable"
        headline = (
            f"{n_failed} of {total} deliveries failed, but no event type failed every time; "
            f"{name}'s retries would likely deliver them"
        )
        lines.append(f"{name} {policy.retries}.")
    if n_failed and policy is not None:
        lines.append(_fix(reasons))
    if policy is not None and policy.caveat and n_failed:
        lines.append(f"Unconfirmed: {policy.caveat}.")
    return {
        "provider": provider,
        "gateway": name,
        "timeout_s": timeout,
        "deliveries": total,
        "failed": n_failed,
        "failure_rate": round(n_failed / total, 4),
        "reasons": dict(reasons),
        "always_failing": sorted(always_failing),
        "verdict": verdict,
        "disable_projected": verdict == "disable",
        "disable_after": policy.disable_after if policy and verdict == "disable" else None,
        "disable_confirmed": bool(policy and policy.disable_confirmed),
        "headline": headline,
        "details": lines,
        "source": policy.source if policy else None,
        "passed": verdict in ("ok", "recoverable", "unknown"),
    }


def outlook_finding(outlook: dict) -> dict:
    """The projection as a graded finding: fails when the gateway would give up or disable."""
    got = f"{outlook['failed']} of {outlook['deliveries']} failed"
    if outlook["always_failing"]:
        got += f"; always: {', '.join(outlook['always_failing'])}"
    return {
        "check": "retry_outlook",
        "severity": "high",
        "passed": outlook["passed"],
        "message": outlook["headline"],
        "details": {"expected": "no event type failing every delivery", "got": got},
    }
