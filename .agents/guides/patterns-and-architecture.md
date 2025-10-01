# Patterns and Architecture

## Principles

These defaults aim to travel well across languages and stacks.
Code samples use Python for brevity—treat them as illustrations you can translate to your tooling.

- Reduce cognitive load with organization
- Prefer composition over inheritance
- Use inheritance when modeling stateful polymorphism
- Functions should be stateless and idempotent
- Classes represent stateful, mutable data
- Classes can namespace stateless functions
- Avoid nesting beyond 4 levels

### Reduce cognitive load with organization

#### Why
- Readers should grasp intent quickly without tracing every line.
- Clear boundaries and names shorten the “what is this?” loop.

#### How
- Separate concerns into small modules/functions with single responsibilities.
- Name things by responsibility and outcome, not by implementation detail.
- Keep entrypoints thin; push detail into helpers.

#### Example (Python)
```python
def process_order(order):
    """Thin orchestration reads like a checklist."""
    validated = validate_order(order)
    priced = price_order(validated)
    payment_result = charge(priced)
    return summarize(payment_result)

def validate_order(order):
    if not order.items:
        raise ValueError("empty order")
    return order

def price_order(order):
    order.total = sum(i.qty * i.price for i in order.items)
    return order

def charge(order):
    # Hidden complexity lives behind a simple name.
    return {"ok": True, "amount": order.total}

def summarize(result):
    return "paid" if result["ok"] else "failed"
```

#### Pitfalls
- Catch-all utils modules; prefer feature-oriented modules.
- Over-abstracting early; extract only when duplication or complexity appears.

### Prefer composition over inheritance

#### Why
- Composition keeps types small, testable, and flexible to change.
- Inheritance couples you to a parent’s lifecycle and surface area.

#### Example (Python; composition)
```python
class Logger:
    def info(self, msg: str) -> None:
        print(f"INFO: {msg}")

class PaymentGateway:
    def charge(self, amount: float) -> bool:
        return amount >= 0

class Checkout:
    def __init__(self, gateway: PaymentGateway, logger: Logger) -> None:
        self.gateway = gateway
        self.logger = logger

    def pay(self, amount: float) -> bool:
        self.logger.info(f"Charging {amount}")
        ok = self.gateway.charge(amount)
        if not ok:
            self.logger.info("Charge failed")
        return ok

# Swap collaborators in tests without subclassing Checkout.
```

#### Counterexample (inheritance used poorly)
```python
class CheckoutLogger(PaymentGateway):
    # Inherits unrelated API; violates LSP and mixes roles.
    def __init__(self, logger: Logger) -> None:
        self.logger = logger
```

### Use inheritance when modeling stateful polymorphism

#### Why
- A stable interface with multiple stateful implementations benefits from inheritance or ABCs.
- Good fit for domain variants that share a contract.

#### Example (Python; ABC-based stateful variants)
```python
from abc import ABC, abstractmethod

class PriceRule(ABC):
    @abstractmethod
    def apply(self, total: float) -> float: ...

class NoDiscount(PriceRule):
    def apply(self, total: float) -> float:
        return total

class PercentageOff(PriceRule):
    def __init__(self, pct: float) -> None:
        self.pct = pct
    def apply(self, total: float) -> float:
        return total * (1 - self.pct)

def checkout(total: float, rule: PriceRule) -> float:
    return rule.apply(total)
```

#### Notes
- Prefer ABCs/protocols for contracts; avoid deep hierarchies.
- In Python, `collections.UserDict`/`UserList` help extend container behavior;
  use your language’s standard library equivalents elsewhere.

### Functions should be stateless and idempotent

#### Why
- Stateless, idempotent functions are easy to test, compose, and cache.

#### Example (Python)
```python
def normalize_email(email: str) -> str:
    """Pure: same input => same output; no side effects."""
    return email.strip().lower()

def upsert_keys(existing: set[str], new: list[str]) -> set[str]:
    """Idempotent: applying twice yields same result as once."""
    return existing | set(new)

# Idempotency check
e1 = upsert_keys({"a"}, ["b", "b"])     # {"a","b"}
e2 = upsert_keys(e1, ["b"])               # still {"a","b"}
```

#### Pitfalls
- Hidden global mutation, time, randomness, or I/O inside “utility” functions.

### Classes represent stateful, mutable data

#### Why
- Encapsulate invariants and lifecycle around changing state.

#### Example (Python)
```python
class RateLimiter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.count = 0

    def allow(self) -> bool:
        if self.count < self.limit:
            self.count += 1
            return True
        return False

    def reset(self) -> None:
        self.count = 0
```

#### Notes
- Leverage your language’s lightweight data-structure helpers (e.g., Python `@dataclass`)
  for simple data holders; add methods when rules emerge.

### Classes can namespace stateless functions

#### Why
- Group related pure operations with shared configuration or naming.

#### Example (Python)
```python
import hashlib

class Password:
    ALGO = "sha256"

    @staticmethod
    def hash(plain: str, salt: str) -> str:
        return hashlib.new(Password.ALGO, f"{salt}:{plain}".encode()).hexdigest()

    @staticmethod
    def verify(plain: str, salt: str, digest: str) -> bool:
        return Password.hash(plain, salt) == digest
```

#### Notes
- Prefer modules or functions unless namespacing adds clarity (config, domain grouping).

### Avoid nesting beyond 4 levels

#### Why
- Deep nesting harms readability and increases cognitive load.

#### How
- Use guard clauses, extract helpers, or dispatch tables/polymorphism.

#### Example (Python; refactor deep nesting)
```python
# Before
def handle(event):
    if event:
        if event.get("type") == "CREATE":
            if event.get("active"):
                do_create(event)

# After
def handle(event):
    if not event or event.get("type") != "CREATE":
        return
    if not event.get("active"):
        return
    do_create(event)
```

#### Alternative: dispatch
```python
def on_create(e): ...
def on_delete(e): ...

DISPATCH = {
    "CREATE": on_create,
    "DELETE": on_delete,
}

def handle(event):
    fn = DISPATCH.get(event.get("type"))
    if fn:
        fn(event)
```


## Testing

### Goals
- Fast, deterministic feedback with clear failure signals.
- Tests describe behavior and contracts, not implementation details.
- High-value coverage: critical paths, invariants, and error handling.

### Levels
- Unit: isolate a function/class; no network, filesystem, or clock.
- Integration: exercise real boundaries (DB, HTTP) with ephemeral resources.
- Contract: verify provider/consumer agreements across modules/services.
- End-to-end: minimal happy-path coverage; keep slow tests few and focused.

### Practices
- Name tests as behavior: `test_<when>_<does>_<expectation>`.
- One assertion per behavior; group related assertions logically.
- Prefer dependency injection + fakes over deep mocking trees.
- Use parameterized test helpers (e.g., `pytest.mark.parametrize`, JUnit params) to cover edge cases concisely.
- Freeze time and randomness (e.g., seed `random`, inject clock/token generators).
- Avoid shared mutable state; prefer fresh fixtures; clean up with context managers.
- Measure coverage but don’t chase 100%; prioritize risk and complexity.

### Example (Python; pytest)
```python
# tests/test_checkout.py
import pytest

@pytest.mark.parametrize(
    "total,pct,expected",
    [
        (100.0, 0.0, 100.0),
        (100.0, 0.10, 90.0),
        (0.0, 0.25, 0.0),
    ],
)
def test_percentage_discount(total, pct, expected):
    from app.pricing import PercentageOff
    rule = PercentageOff(pct)
    assert rule.apply(total) == expected

def test_checkout_integration(tmp_path):
    # Use temp folder as ephemeral resource; no globals.
    from app.checkout import Checkout
    from app.gateways import FileGateway

    gw = FileGateway(root=tmp_path)
    co = Checkout(gateway=gw)
    assert co.pay(42.0) is True
```
### Fakes, not mocks (example)

_Python illustration; employ hand-rolled fakes in your framework to capture behavior without brittle mocks._

```python
class FakeGateway:
    def __init__(self):
        self.charges = []
    def charge(self, amount: float) -> bool:
        self.charges.append(amount)
        return True

def test_checkout_records_charge():
    from app.checkout import Checkout
    co = Checkout(gateway=FakeGateway())
    assert co.pay(10.0)
```

### Property-based spot checks
- Use property-based testing tools (Hypothesis, QuickCheck, jqwik, etc.) where they reinforce invariants.
- Examples: parsing/serialization round-trips, commutativity, idempotency.


## Security Practices

### Principles
- Treat all input as untrusted; validate at boundaries and before use.
- Least privilege for processes, data, and credentials; deny by default.
- Never log secrets or PII; redact aggressively and centralize logging.
- Prefer secure defaults: timeouts, TLS verification, prepared statements.

### Data handling
- Validation: use schemas or typed validators for external inputs (HTTP, CLI, files).
- Deserialization: avoid `pickle`/`eval`; prefer JSON or structured models.
- File paths: normalize and restrict to allowed roots to prevent traversal.

### Secrets
- Store secrets in env vars or a secret manager; never in VCS.
- Rotate credentials; support multiple keys if feasible.
- Compare secrets with `hmac.compare_digest` to avoid timing attacks.

### Example (Python)
```python
# SQL: parameterized, never string formatting
cur.execute("INSERT INTO users(name) VALUES (?)", (name,))

# HTTP: set timeouts and verify TLS
import requests
resp = requests.get(url, timeout=5, verify=True)
resp.raise_for_status()

# Secrets: generate and compare safely
import secrets, hmac
token = secrets.token_urlsafe(32)
assert hmac.compare_digest(token, token)

# Files: safe joining
from pathlib import Path
def safe_join(root: Path, relative: str) -> Path:
    p = (root / relative).resolve()
    if root not in p.parents and p != root:
        raise ValueError("path traversal")
    return p
```

### Logging and privacy
- Avoid logging raw requests, bodies, or headers with credentials.
- Introduce explicit whitelists of safe fields to log.

### Operational hardening
- Set process umask appropriately; restrict file permissions on writes.
- Define HTTP client/server timeouts; implement circuit breakers/retries with backoff.
- Keep dependencies updated; enable SCA/SAST scanners in CI where applicable.


## Optimization and Trade-offs

### Principles
- Measure, don’t guess: profile hot paths before optimizing.
- Optimize algorithms and data structures first; micro-optimizations last.
- Trade-offs are explicit: readability vs speed, memory vs CPU, latency vs throughput.

### Techniques
- Use generators/iterators to stream instead of loading everything in memory.
- Cache pure/costly functions with `functools.lru_cache` when hit rate is high.
- Batch work to amortize overhead (DB writes, network calls, disk I/O).
- Choose concurrency based on workload: threads/asyncio for I/O-bound, processes for CPU-bound.
- Short-circuit and guard early; avoid repeated computation.

### Python examples
```python
# LRU cache for pure computation
from functools import lru_cache

@lru_cache(maxsize=1024)
def parse_schema(schema_text: str) -> dict:
    ...

# Streaming processing
def process_lines(path):
    with open(path, "rt") as fh:
        for line in fh:  # constant memory
            yield transform(line)

# Batching
def chunked(iterable, size):
    chunk = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) == size:
            yield chunk; chunk = []
    if chunk:
        yield chunk
```

### When not to optimize
- If the code runs infrequently and is clear, prefer clarity.
- If complexity risks bugs exceeding saved CPU time.
- If the bottleneck is elsewhere (I/O latency, external service).


## Logging and Error Handling

### Goals
- Observability that helps debug production issues quickly without leaking secrets.
- Consistent error semantics: domain errors are explicit, unexpected errors crash fast and loud.

### Logging
- Structure logs (JSON or key-value) and include stable fields: `event`, `component`, `request_id`.
- Use levels intentionally: `DEBUG` (dev detail), `INFO` (state changes),
  `WARNING` (recoverable oddities), `ERROR` (failed operation), `CRITICAL` (system unusable).
- Log once at the boundary; avoid duplicate logs for the same failure.
- Redact secrets before logging; centralize redaction helpers.

_Python illustration; configure logging analogously in your stack._

```python
import logging
logger = logging.getLogger("app.checkout")

# Configure once at entrypoint
# logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

def pay(amount: float, *, request_id: str):
    logger.info("event=checkout_start amount=%s request_id=%s", amount, request_id)
    ...
```

### Error handling
- Library/internal code raises exceptions; callers decide to handle or propagate.
- Wrap external failures in domain-specific exceptions; preserve original context.
- Avoid returning `None`/sentinels for exceptional states; raise with message and data.
- Use retries with backoff for transient errors; cap attempts and total time.

_Python illustration; translate exception handling patterns to your language._

```python
class PaymentError(Exception):
    pass

def charge(gw, amount: float):
    try:
        return gw.charge(amount)
    except TimeoutError as exc:
        raise PaymentError("gateway timeout") from exc
```

## Module and Package Layout

### Principles
- Organize by feature/domain, not by technical type alone.
- Keep public APIs small; hide internals behind modules or `_` prefixes.
- Prefer absolute imports for clarity; avoid deep relative chains.

### Example layout (Python package; feature-oriented)
```
src/app/
  __init__.py
  checkout/
    __init__.py
    service.py         # orchestration
    gateways.py        # integrations/ports
    models.py          # dataclasses/entities
    errors.py
  pricing/
    __init__.py
    rules.py
  lib/
    logging.py         # shared infra (minimal)
    time.py            # clock abstraction
tests/
  checkout/
    test_service.py
  pricing/
    test_rules.py
```

### Conventions
- In Python packages, let `__init__.py` expose a minimal public surface via `__all__` if needed;
  mirror that minimalism in other module systems.
- Avoid giant `utils.py`; instead create `lib/<area>.py` or feature-local helpers.
- Keep entrypoints (`cli.py`, `main.py`) thin; configure and call feature services.


## Project-specific guidance

_Capture repository or team-specific architectural rules here (framework integrations,
layering constraints, approved libraries, etc.)._


## Anti-patterns and Micro-patterns

### Anti-patterns
- Mutable default arguments: `def f(x=[])` → use `None` and create inside.
- God objects / manager classes with unclear boundaries.
- Boolean flags controlling behavior branches: split into separate functions or strategies.
- Catch-all `except Exception:` that hide failures; always log/raise with context.
- Deep inheritance trees; prefer composition or protocols.
- Hidden I/O in “pure” utilities; declare effects or move to adapters.
- Over-mocking internal details; coupled tests that break on refactors.
- Premature optimization that obscures intent without measured need.
- Global singletons/state; prefer passing dependencies explicitly.

### Micro-patterns
- Guard clauses: return early to flatten nesting and clarify preconditions.
- Strategy: pass behavior (callables/objects) instead of conditionals.
- Adapter: wrap third-party APIs to present a stable, testable interface.
- Null object: provide do-nothing implementation to avoid `if x is None`.
- Context manager: ensure resources are released (`with` for files, locks, sessions).
- Use lightweight value-object helpers (e.g., data classes) and validate immediately after initialization.
- Sentinel object for “not provided” distinct from `None`.
- Prefer standard library facilities for paths, timezones, and enums (e.g., Python `pathlib.Path`, `datetime`, `Enum`).

_Python illustration; adapt idioms to your language._

```python
# Mutable default safe pattern
from dataclasses import dataclass

_MISSING = object()

def add_item(item, items=None):
    items = [] if items is None else list(items)
    items.append(item)
    return items

@dataclass(frozen=True)
class Money:
    amount: int
    currency: str = "USD"
```
