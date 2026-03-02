# Carbon Layer — Technical Requirements

## What We're Building

**Chaos engineering for payment flows.**

A scenario testing engine that simulates real-world payment failure modes —
dispute spikes, payment failures, refund storms, settlement delays — against
your live payment integration, then tells you what your system handled
correctly and what broke.

Connect your Razorpay (or Stripe/Cashfree) test account, pick a scenario,
run it, and discover the gaps in your payment handling before your customers do.

---

## The Problem

Every company processing payments has the same blind spot: they test the
happy path (payment succeeds → order fulfilled) and ship. Then in production:

- A dispute spike hits and their system doesn't auto-respond → they lose ₹5L in chargebacks
- Payment gateway returns an unexpected error code → orders get stuck in "processing" forever
- A customer triggers 3 partial refunds → the reconciliation breaks
- Flash sale drives 10x volume → webhook queue backs up, payments are captured but orders aren't updated

These aren't hypothetical. They happen to every company processing payments
at scale. The problem is **nobody tests for them** because setting up these
scenarios manually is tedious, and no tool exists to do it automatically.

Carbon Layer is that tool.

---

## How It Works

```
1. Connect: Point Carbon Layer at your Razorpay test account
2. Pick:    Choose a scenario (dispute spike, payment failures, refund storm, etc.)
3. Run:     Carbon Layer executes the scenario against Razorpay's test API
4. React:   Your application receives webhooks / reads API — does it handle it?
5. Report:  Carbon Layer validates the resulting state and reports what passed/failed
```

### Example: "Dispute Spike" Scenario

```bash
carbon run dispute-spike \
  --provider razorpay \
  --api-key rzp_test_xxxxx \
  --api-secret yyyyy \
  --baseline-orders 100 \
  --dispute-rate 0.15 \
  --seed 42
```

**What happens:**
1. Creates 100 orders with successful payments (baseline)
2. Triggers disputes on 15 of them (normally you'd see 1-2)
3. Waits for your system to receive Razorpay webhooks
4. Checks: Did your system flag the spike? Did it auto-respond? Did it alert?
5. Produces a report:

```
┌─────────────────────────────────────────────────────┐
│  Scenario: dispute-spike                             │
│  Provider: razorpay (test mode)                      │
├─────────────────────────────────────────────────────┤
│  Orders created:        100                          │
│  Payments captured:     100                          │
│  Disputes created:      15                           │
│                                                      │
│  ⚠ FINDINGS:                                        │
│  • 15 disputes created, 0 auto-responded             │
│  • No evidence submitted within SLA window            │
│  • Estimated loss if production: ₹2,34,000            │
│                                                      │
│  Razorpay state verification:                        │
│  ✓ All orders in correct state                       │
│  ✓ All payments captured                             │
│  ✗ 15 disputes in "open" — none moved to "under_review"│
└─────────────────────────────────────────────────────┘
```

---

## Scenario Library

### Category 1: Payment Failures

| Scenario | What It Tests | Actions |
|----------|--------------|---------|
| `payment-decline-spike` | 30% of payments fail (card declined, UPI timeout) | Create orders → attempt payments → 30% fail → verify retry logic |
| `authorization-timeout` | Payments authorized but never captured | Create orders → authorize → wait → check for abandoned auths |
| `double-payment` | Same order gets two payment attempts | Create order → two concurrent payments → verify idempotency |
| `gateway-error-burst` | Intermittent gateway errors mid-checkout | Create orders → payments succeed/fail randomly → verify order states |

### Category 2: Refund Chaos

| Scenario | What It Tests | Actions |
|----------|--------------|---------|
| `refund-storm` | 25% refund rate sudden spike | Normal orders → mass refund requests → verify accounting |
| `partial-refund-chain` | Multiple partial refunds on same payment | Payment captured → partial refund → another partial → verify totals |
| `refund-exceeds-amount` | Refund request > payment amount | Payment captured → attempt over-refund → verify rejection handling |
| `refund-on-disputed` | Refund attempted on payment already under dispute | Payment → dispute → refund attempt → verify conflict handling |

### Category 3: Dispute / Chargeback

| Scenario | What It Tests | Actions |
|----------|--------------|---------|
| `dispute-spike` | 15% dispute rate (vs normal 1-2%) | Baseline payments → mass disputes → check response time |
| `dispute-evidence-deadline` | Disputes approaching evidence deadline | Create disputes → verify evidence submission within window |
| `lost-dispute-accounting` | Dispute lost, funds debited | Dispute → mark lost → verify merchant balance adjustment |
| `dispute-on-refunded` | Dispute opened on already-refunded payment | Refund → dispute → verify correct handling |

### Category 4: Volume / Load

| Scenario | What It Tests | Actions |
|----------|--------------|---------|
| `flash-sale` | 10x normal order volume in 1 hour | Burst of orders/payments → verify webhook processing keeps up |
| `steady-ramp` | Gradual volume increase over days | Increasing daily volume → verify scaling behavior |
| `payment-method-shift` | UPI suddenly dominant (90% UPI) | Shift payment method distribution → verify method-specific flows |

### Category 5: Edge Cases

| Scenario | What It Tests | Actions |
|----------|--------------|---------|
| `minimum-amount` | ₹1 transactions | Create very small orders → verify handling |
| `maximum-amount` | ₹10L+ transactions | Create large orders → verify limits/approvals |
| `repeat-customer-heavy` | 80% orders from 5 customers | Concentrated customer activity → verify dedup/fraud logic |
| `currency-edge` | Paise rounding issues | Odd amounts (₹99.99 = 9999 paise) → verify rounding |

### Category 6: Compliance Readiness (Future — Phase 3)

| Scenario | What It Tests | Actions |
|----------|--------------|---------|
| `rbi-refund-sla` | Refunds processed within RBI mandate (5-7 days) | Create refunds → verify processing time |
| `pci-data-handling` | Sensitive data not logged/stored | Run scenarios → check your logs for PAN/CVV leaks |
| `settlement-reconciliation` | T+2 settlement matches captured payments | Payments → settlements → verify reconciliation accuracy |

---

## System Architecture

```
┌────────────────────────────────────────────────────────────┐
│                      CLI (Typer + Rich)                      │
│  carbon run <scenario> --provider razorpay                   │
│  carbon scenarios list                                       │
│  carbon report --run-id abc123                               │
│  carbon validate --run-id abc123                             │
└────────────────────────────┬───────────────────────────────┘
                             │
          ┌──────────────────▼──────────────────┐
          │        Scenario Registry             │
          │  Built-in scenarios (YAML defined)   │
          │  Custom user scenarios               │
          │  Scenario parameters + overrides     │
          └──────────────────┬──────────────────┘
                             │
          ┌──────────────────▼──────────────────┐
          │        Scenario Compiler             │
          │  Scenario def + domain graph         │
          │  → Execution Plan (DAG)              │
          │  Distributions, entity linking        │
          └──────────────────┬──────────────────┘
                             │
          ┌──────────────────▼──────────────────┐
          │       Domain Graph Engine            │
          │  Payment lifecycle state machines     │
          │  Loaded from YAML                     │
          │  Entities + transitions + constraints │
          └──────────────────┬──────────────────┘
                             │
          ┌──────────────────▼──────────────────┐
          │       Execution Engine               │
          │  Topological sort → run actions       │
          │  Entity ID resolution (ref: → real)   │
          │  Rate limiting + retry                │
          └───────────┬──────────┬──────────────┘
                      │          │
          ┌───────────▼──┐  ┌───▼──────────────┐
          │  Adapter      │  │  Storage          │
          │  Layer        │  │  (SQLite)         │
          │               │  │                   │
          │  ┌──────────┐ │  │  runs             │
          │  │Razorpay  │ │  │  actions           │
          │  │(MVP)     │ │  │  entity_map        │
          │  ├──────────┤ │  │  findings          │
          │  │Stripe    │ │  │                   │
          │  │(v2)      │ │  └───────────────────┘
          │  ├──────────┤ │
          │  │Cashfree  │ │
          │  │(v3)      │ │
          │  └──────────┘ │
          └───────────┬───┘
                      │
          ┌───────────▼──────────────────────┐
          │       Validator + Reporter        │
          │                                    │
          │  Fetch actual state from provider   │
          │  Compare expected vs actual         │
          │  Generate findings                  │
          │  Estimate production impact          │
          │  Produce terminal report             │
          └────────────────────────────────────┘
```

---

## Scenario Definition Format

Scenarios are defined in YAML. Users can use built-in ones or create custom:

```yaml
# scenarios/dispute-spike.yaml
name: dispute-spike
description: >
  Simulates a sudden spike in payment disputes (15% vs normal 1-2%).
  Tests whether your system detects the anomaly, auto-responds with
  evidence, and alerts your team.

category: dispute

parameters:
  baseline_orders:
    default: 100
    description: Number of normal orders to create as baseline
  dispute_rate:
    default: 0.15
    min: 0.05
    max: 0.50
    description: Percentage of payments that receive disputes
  payment_method:
    default: mixed
    options: [upi, card, netbanking, mixed]
  avg_order_value:
    default: 2500
    description: Average order value in INR

phases:
  - name: baseline
    description: Create normal order + payment flow
    actions:
      - create_customers:
          count: "{baseline_orders * 0.3}"  # 30 unique customers for 100 orders
      - create_orders:
          count: "{baseline_orders}"
          link_to: customers
          amount_distribution: lognormal
          avg_amount: "{avg_order_value}"
      - create_payments:
          for_each: orders
          success_rate: 0.95
          method_distribution: "{payment_method}"
      - capture_payments:
          for_each: successful_payments

  - name: attack
    description: Trigger dispute spike
    depends_on: baseline
    actions:
      - create_disputes:
          count: "{baseline_orders * dispute_rate}"
          on: captured_payments
          sample: random
          dispute_reason: [chargeback, fraud, not_received, duplicate]

  - name: observe
    description: Wait and check system response
    depends_on: attack
    wait: 10s  # Give webhooks time to deliver
    actions: []

validations:
  - check: dispute_response_rate
    description: What % of disputes received evidence submission
    expected: ">0.8"
    severity: critical

  - check: all_orders_consistent
    description: Order states are internally consistent
    expected: true
    severity: high

  - check: no_orphaned_payments
    description: Every payment maps to a valid order
    expected: true
    severity: high

findings:
  - condition: "dispute_response_rate < 0.5"
    message: "Less than 50% of disputes auto-responded. Estimated loss in production: ₹{unresponded_disputes * avg_order_value}"
    severity: critical

  - condition: "dispute_response_rate == 0"
    message: "No disputes were responded to. Your system has no dispute handling."
    severity: critical
```

---

## Custom Scenario Example

Users can compose their own:

```yaml
# my-scenarios/black-friday.yaml
name: black-friday-sim
description: Simulate Black Friday traffic pattern

parameters:
  peak_orders_per_minute: 200
  duration_hours: 4
  failure_rate: 0.12  # higher during peak

phases:
  - name: warmup
    actions:
      - create_orders:
          count: 50
          rate: 10/min
      - create_payments:
          for_each: orders
          success_rate: 0.95

  - name: peak
    depends_on: warmup
    actions:
      - create_orders:
          count: "{peak_orders_per_minute * 60 * duration_hours}"
          rate: "{peak_orders_per_minute}/min"
      - create_payments:
          for_each: orders
          success_rate: "{1 - failure_rate}"

  - name: cooldown
    depends_on: peak
    actions:
      - create_refunds:
          count: "{peak.orders.count * 0.15}"
          on: peak.captured_payments

validations:
  - check: all_payments_settled
    description: No stuck payments after cooldown
    expected: true
```

---

## Adapter Protocol

```python
@runtime_checkable
class PaymentAdapter(Protocol):
    """Every payment provider implements this interface."""

    provider_name: str

    # Connection
    async def validate_connection(self) -> bool: ...

    # Write operations
    async def create_customer(self, params: dict) -> dict: ...
    async def create_order(self, params: dict) -> dict: ...
    async def create_payment(self, order_id: str, params: dict) -> dict: ...
    async def capture_payment(self, payment_id: str, amount: int) -> dict: ...
    async def create_refund(self, payment_id: str, params: dict) -> dict: ...
    async def create_dispute(self, payment_id: str, params: dict) -> dict: ...

    # Read operations (for validation)
    async def fetch_order(self, order_id: str) -> dict: ...
    async def fetch_payment(self, payment_id: str) -> dict: ...
    async def list_disputes(self, filters: dict) -> list[dict]: ...
    async def list_refunds(self, payment_id: str) -> list[dict]: ...
```

Adding a new provider = implement the protocol, register with `@AdapterRegistry.register("stripe")`.

---

## Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | Python 3.11+ | Async I/O, your stack, fastest to ship |
| Storage | SQLite (aiosqlite) | Zero infra, CLI-friendly, ACID |
| Graph Engine | NetworkX + YAML | Domain graphs are small, YAML is readable |
| HTTP Client | httpx (async) | Connection pooling, native async |
| CLI | Typer + Rich | Progress bars, tables, colored reports |
| Data Realism | Faker `en_IN` + curated lists | Indian names, phones, amounts |
| Config | .env + pydantic-settings | API keys in .env, scenarios in YAML |
| Retry | tenacity | Exponential backoff with jitter |

---

## Project Structure

```
carbon-layer/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
│
├── domains/                        # Domain lifecycle graphs
│   └── payment.yaml                # Payment lifecycle (provider-agnostic)
│
├── scenarios/                      # Built-in scenario library
│   ├── payment-failures/
│   │   ├── payment-decline-spike.yaml
│   │   ├── authorization-timeout.yaml
│   │   └── double-payment.yaml
│   ├── refund-chaos/
│   │   ├── refund-storm.yaml
│   │   ├── partial-refund-chain.yaml
│   │   └── refund-exceeds-amount.yaml
│   ├── disputes/
│   │   ├── dispute-spike.yaml
│   │   ├── dispute-evidence-deadline.yaml
│   │   └── lost-dispute-accounting.yaml
│   ├── volume/
│   │   ├── flash-sale.yaml
│   │   └── steady-ramp.yaml
│   └── edge-cases/
│       ├── minimum-amount.yaml
│       ├── maximum-amount.yaml
│       └── repeat-customer-heavy.yaml
│
├── src/
│   └── carbon/
│       ├── __init__.py
│       ├── cli.py                  # Typer CLI
│       ├── config.py               # pydantic-settings
│       │
│       ├── graph/                  # Domain graph engine
│       │   ├── __init__.py
│       │   ├── models.py           # StateNode, Transition, EntityGraph
│       │   └── loader.py           # YAML → NetworkX
│       │
│       ├── scenarios/              # Scenario engine
│       │   ├── __init__.py
│       │   ├── registry.py         # Discover + load scenarios
│       │   ├── models.py           # Scenario, Phase, Validation, Finding
│       │   └── parser.py           # YAML → Scenario objects
│       │
│       ├── compiler/               # Scenario → Execution Plan
│       │   ├── __init__.py
│       │   ├── compiler.py         # Scenario + graph → ExecutionPlan
│       │   ├── planner.py          # Action generation + dependency wiring
│       │   └── distributions.py    # Amount, temporal, method distributions
│       │
│       ├── engine/                 # Execution engine
│       │   ├── __init__.py
│       │   ├── executor.py         # DAG runner
│       │   ├── scheduler.py        # Topological sort
│       │   ├── resolver.py         # Entity ID resolution (ref: → real)
│       │   └── retry.py            # Retry + rate limiter
│       │
│       ├── adapters/               # Payment provider adapters
│       │   ├── __init__.py
│       │   ├── base.py             # PaymentAdapter protocol
│       │   ├── registry.py         # Adapter registry
│       │   ├── razorpay/
│       │   │   ├── __init__.py
│       │   │   ├── client.py       # Razorpay API client
│       │   │   ├── adapter.py      # PaymentAdapter impl
│       │   │   └── mapping.py      # Field mapping
│       │   └── stripe/             # Future
│       │       └── ...
│       │
│       ├── validator/              # Post-execution validation
│       │   ├── __init__.py
│       │   ├── validator.py        # State verification
│       │   ├── checks.py           # Built-in validation checks
│       │   └── findings.py         # Finding generation + severity
│       │
│       ├── reporter/               # Output formatting
│       │   ├── __init__.py
│       │   ├── terminal.py         # Rich terminal report
│       │   └── json_report.py      # JSON export
│       │
│       ├── data/                   # Data realism
│       │   ├── __init__.py
│       │   ├── generators.py       # Faker en_IN + custom providers
│       │   └── indian_data.py      # Curated Indian data
│       │
│       └── storage/
│           ├── __init__.py
│           ├── db.py               # SQLite init
│           └── models.py           # Run, Action, EntityMap, Finding
│
└── tests/
    ├── conftest.py
    ├── test_graph/
    ├── test_scenarios/
    ├── test_compiler/
    ├── test_engine/
    └── test_adapters/
```

---

## SQLite Schema

```sql
CREATE TABLE runs (
    id              TEXT PRIMARY KEY,
    scenario_name   TEXT NOT NULL,
    provider        TEXT NOT NULL,
    parameters      JSON NOT NULL,
    status          TEXT NOT NULL,       -- pending | running | completed | failed
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    summary         JSON
);

CREATE TABLE actions (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id),
    phase           TEXT NOT NULL,
    action_type     TEXT NOT NULL,
    parameters      JSON NOT NULL,
    dependencies    JSON,
    status          TEXT NOT NULL,
    result          JSON,
    error           TEXT,
    executed_at     TIMESTAMP,
    retry_count     INTEGER DEFAULT 0
);

CREATE TABLE entity_map (
    run_id          TEXT NOT NULL REFERENCES runs(id),
    local_id        TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    remote_id       TEXT,
    provider        TEXT NOT NULL,
    state           TEXT,
    metadata        JSON,
    created_at      TIMESTAMP,
    PRIMARY KEY (run_id, local_id)
);

CREATE TABLE findings (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id),
    check_name      TEXT NOT NULL,
    severity        TEXT NOT NULL,       -- critical | high | medium | low
    passed          BOOLEAN NOT NULL,
    message         TEXT,
    details         JSON,
    created_at      TIMESTAMP
);

CREATE INDEX idx_actions_run ON actions(run_id);
CREATE INDEX idx_findings_run ON findings(run_id);
CREATE INDEX idx_findings_severity ON findings(severity);
```

---

## CLI Commands

```bash
# List available scenarios
carbon scenarios list

# Show scenario details
carbon scenarios show dispute-spike

# Run a scenario
carbon run dispute-spike \
  --provider razorpay \
  --api-key rzp_test_xxx \
  --api-secret yyy \
  --baseline-orders 100

# Dry run (compile plan, show what would happen, don't execute)
carbon run dispute-spike --dry-run

# Check status of a running/completed scenario
carbon status --run-id abc123

# View detailed report
carbon report --run-id abc123

# Validate state (re-check provider state after some time)
carbon validate --run-id abc123

# Clean up entities created by a run
carbon clean --run-id abc123

# Run with custom scenario file
carbon run --file ./my-scenarios/black-friday.yaml --provider razorpay
```

---

## Dependencies

```toml
[project]
name = "carbon-layer"
version = "0.1.0"
requires-python = ">=3.11"

dependencies = [
    "typer[all]",
    "httpx",
    "pydantic>=2.0",
    "pydantic-settings",
    "networkx",
    "pyyaml",
    "faker",
    "aiosqlite",
    "tenacity",
    "rich",
]

[project.optional-dependencies]
dev = [
    "pytest",
    "pytest-asyncio",
    "ruff",
    "respx",
]

[project.scripts]
carbon = "carbon.cli:app"
```

---

## Phased Roadmap

### Phase 1: Scenario Testing Engine (Weeks 1-4) ← WE ARE HERE
- Core graph engine + YAML loader
- Scenario registry + parser
- Scenario compiler (scenario → execution DAG)
- Execution engine (sequential, retry, rate limiting)
- Razorpay adapter (orders, payments, refunds, disputes)
- Validator + findings engine
- CLI: run, scenarios list, report
- 5 built-in scenarios (1 per category)
- Terminal report with Rich

### Phase 2: Environment-as-a-Service (Weeks 5-8)
- `carbon populate` command (fill sandbox with realistic history)
- Business profile templates (ecommerce, saas, marketplace)
- Temporal + amount distributions
- Data realism (Faker en_IN, curated Indian data)
- `carbon clean` command
- Resumable runs (pause + continue)

### Phase 3: Compliance & Audit Simulation (Weeks 9-12)
- Compliance scenario suites (RBI, PCI-DSS readiness)
- Structured compliance reports (PDF/HTML export)
- Evidence collection (log what happened, when)
- `carbon compliance run --suite rbi-refund-sla`
- Audit trail with timestamps and checksums

### Phase 4: Multi-Provider + Scale (Weeks 13+)
- Stripe adapter (with timestamp backdating)
- Cashfree adapter
- Webhook simulation (send events to user's endpoint)
- Parallel execution for large scenarios
- Web UI for scenario design + reports

---

## What's NOT in Phase 1

- Web UI
- Stripe/Cashfree adapters
- Webhook simulation
- Parallel execution
- Compliance reports
- Environment population (that's Phase 2)
- LLM-generated data
- Multi-user support
