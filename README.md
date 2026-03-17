# Carbon Layer

**Chaos engineering for payment flows.**

Test what breaks in your payment integration before it breaks in production. Run scenarios (dispute spikes, refund storms, payment failures) against your Razorpay test account and get a report on what your system handled—and what it didn't.

## Status

Early stage. MVP implemented: scenario engine, mock + Razorpay adapters, CLI.

**Run locally (no Razorpay account):**
```bash
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/carbon scenarios-list
.venv/bin/carbon run dispute-spike --provider mock
.venv/bin/carbon report --run-id <run_id>
```

**Webhook simulation:**

Point Carbon Layer at your webhook endpoint and it will fire Razorpay-format events (`payment.captured`, `dispute.created`, `refund.processed`, etc.) at it after the scenario runs. The report shows how your endpoint responded — 2xx, 4xx, 5xx, or timeout — for each event type.

```bash
carbon run dispute-spike --provider mock --webhook-url http://localhost:8000/webhooks/razorpay
```

No Razorpay account needed. Carbon generates the payloads and signs them with `X-Razorpay-Signature` (HMAC-SHA256). Pass `--webhook-url` to any scenario.

**Database:** PostgreSQL only. Set `DATABASE_URL` (default: `postgresql://localhost:5432/carbon`). Create the database first: `createdb carbon`. Tables are created on first run. If you see "password authentication failed", either set `DATABASE_URL` in `.env` with your credentials, or use Docker: `docker run -d --name carbon-pg -e POSTGRES_PASSWORD=carbon -e POSTGRES_DB=carbon -p 5433:5432 postgres:15` then `DATABASE_URL=postgresql://postgres:carbon@localhost:5433/carbon` (see `.env.example`; a `.env` with this URL is created for local runs).

**Base SLA (mock adapter, PostgreSQL, real-world scale):** Target: **&lt; 5 s (5,000 ms)** end-to-end per scenario. Measured after optimization (single connection, bulk entity inserts, parallel adapter calls).

| Scenario | Scale | Result | Measured (ms) | Target |
|----------|--------|--------|---------------|--------|
| dispute-spike | 1,000 orders, 1,000 captured, 150 disputes | All passed | ~440 ms | &lt; 5,000 ms ✓ |
| payment-decline-spike | 2,000 orders, ~1,400 captured (70% success) | All passed | ~480 ms | &lt; 5,000 ms ✓ |
| refund-storm | 2,000 orders, 2,000 captured, 500 refunds | All passed | ~520 ms | &lt; 5,000 ms ✓ |


**Tests:**
```bash
pip install -e ".[dev]"
pytest tests/ -v                    # unit tests only (no DB)
DATABASE_URL=postgresql://... pytest tests/ -v   # include integration tests (all 7 scenarios)
```
Unit tests cover the validator (metrics, expected evaluation, conditions) and compiler (scenario load and plan). Integration tests run each scenario end-to-end with the mock adapter and assert entity counts and that high-severity findings pass; they require PostgreSQL.

## Roadmap

- **Phase 1:** Scenario testing engine (Razorpay adapter)
- **Phase 2:** Environment-as-a-Service (populate sandbox with realistic data)
- **Phase 3:** Compliance & audit simulation

## License

Apache 2.0 — see [LICENSE](./LICENSE).
