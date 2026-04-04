# Data Flow Analysis Framework — Deep Technical Report

## 1. Overview

The `slither/analyses/data_flow/` module is a forward abstract interpretation
framework that computes integer value ranges for Solidity smart contracts. It
operates on Slither's SSA IR, builds Z3 bitvector constraints for each
operation, and queries Z3 Optimize to extract tight [min, max] bounds per
variable. The output is annotated source code showing computed ranges inline.

There are two analyses in this directory:

- **Interval analysis** (`analyses/interval/`) — the primary, mature analysis.
  Computes integer ranges via SMT constraint accumulation and Z3 Optimize
  queries. This report focuses here.
- **Rounding analysis** (`analyses/rounding/`) — in-progress, untracked.
  Detects rounding direction mismatches in DeFi fixed-point math. Separate
  concern, separate code.

A **legacy interval analysis** (`analyses/interval_legacy/`) is preserved but
not actively used. It had memory safety checking, mload/mstore handlers, and
threshold-based widening that were removed in the v2 rewrite.

---

## 2. Directory Structure

```
data_flow/
├── __init__.py
├── run_analysis.py          # CLI entry point, orchestration, annotation
├── analysis.py              # Range solving (solve_variable_range)
├── analysis_models.py       # Output data models (LineAnnotation, etc.)
├── source_view.py           # Rich terminal rendering
│
├── engine/
│   ├── engine.py            # Worklist-based dataflow engine
│   └── direction.py         # Forward direction, transfer function dispatch
│
├── smt_solver/
│   ├── solver.py            # Abstract SMTSolver interface
│   ├── types.py             # Sort, SMTVariable, SMTTerm, CheckSatResult
│   ├── cache.py             # LRU cache for range queries
│   ├── telemetry.py         # Performance/precision metrics
│   └── strategies/
│       └── z3_solver.py     # Z3 implementation (757 lines)
│
├── analyses/
│   ├── interval/            # Current interval analysis (v2)
│   │   ├── analysis/
│   │   │   ├── analysis.py  # IntervalAnalysis class
│   │   │   └── domain.py    # IntervalDomain lattice
│   │   ├── core/
│   │   │   ├── state.py     # State: variables, comparisons, path constraints
│   │   │   └── tracked_variable.py  # TrackedSMTVariable wrapper
│   │   ├── operations/      # 20+ operation handlers
│   │   │   ├── registry.py
│   │   │   ├── base.py      # OperandResolver mixin
│   │   │   ├── assignment.py
│   │   │   ├── binary/      # arithmetic.py, comparison.py
│   │   │   ├── phi.py, phi_callback.py
│   │   │   ├── solidity_call/  # require_assert, abi, sload, sstore, gasleft
│   │   │   ├── type_conversion.py, unary.py
│   │   │   ├── width_matching.py, type_utils.py
│   │   │   └── (index, length, member, delete, etc.)
│   │   └── LIMITATIONS.md
│   │
│   ├── interval_legacy/     # Legacy v1 (preserved, unused)
│   └── interval_v2_design.md
│
├── logger/
│   └── logger.py            # DataFlowLogger singleton (loguru-based)
│
└── analyses/rounding/       # Rounding analysis (in-progress, untracked)
```

---

## 3. Pipeline: Start to Finish

### 3.1 Input

A Solidity source file path. The CLI (`run_analysis.py`) compiles it via
`Slither(path)` which invokes `solc`. Optional filters: `-c ContractName`,
`-f functionName`. Analysis runs **per function**.

### 3.2 IR Representation

Slither's **SSA form** over its own IR. For:

```solidity
function f(uint256 a) public pure returns (uint256) {
    uint256 b = a + 1;
    return b;
}
```

The SSA IR is:

```
Node VARIABLE (line 5):
    Binary:     TMP_0(uint256) = a_1 (c)+ 1
    Assignment: b_1(uint256) := TMP_0(uint256)

Node RETURN (line 6):
    Return: RETURN b_1
```

Variables get SSA suffixes (`a_1`, `b_1`). Intermediate results get `TMP_`
names. Each IR operation has a type class (`Binary`, `Assignment`, `Return`,
`Phi`, `SolidityCall`, `Condition`, etc.).

The CFG is Slither's control flow graph with node types including
`ENTRYPOINT`, `VARIABLE`, `EXPRESSION`, `RETURN`, `IFLOOP` (loop header),
`STARTLOOP`, `ENDLOOP`, and conditional nodes with `son_true`/`son_false`.

### 3.3 Analysis Phase (Forward Dataflow)

#### Engine (`engine/engine.py`)

The `Engine` class implements a standard worklist fixpoint algorithm:

```
1. Initialize: all nodes get pre=BOTTOM, post=BOTTOM
2. Add entry point to worklist (FIFO)
3. While worklist not empty (safety limit: 10,000 iterations):
   a. Pop node from front
   b. Apply transfer function to each SSA IR operation
   c. Set post = pre (after all operations)
   d. Propagate to successors:
      - If conditional branch: narrow via apply_condition()
      - If back edge (son is IFLOOP): apply widening
      - Join with successor's pre-state
      - If changed: add successor to worklist
4. Return dict[Node, AnalysisState] with pre/post per node
```

Node visit tracking warns at 50 visits, errors at 100 (infinite loop
detection).

#### Transfer Function Dispatch

`IntervalAnalysis.transfer_function()` dispatches each IR operation to a
handler via the `OperationHandlerRegistry`:

```
IR Class              → Handler
─────────────────────────────────────
Assignment            → AssignmentHandler
Binary (+,-,*,/,%)    → ArithmeticHandler
Binary (<,>,==,!=)    → ComparisonHandler
Unary (!,~,-)         → UnaryHandler
TypeConversion        → TypeConversionHandler
Phi                   → PhiHandler
Condition             → ConditionHandler
SolidityCall(require) → RequireAssertHandler
SolidityCall(abi.*)   → AbiHandler
SolidityCall(sload)   → SloadHandler
SolidityCall(sstore)  → SstoreHandler
InternalCall          → InterproceduralHandler
LibraryCall           → InterproceduralHandler
HighLevelCall         → HighLevelCallHandler
Return                → ReturnHandler
Index, Length, Member  → (create unconstrained)
```

#### How Constraints Are Built

Each handler follows the same pattern:

1. **Resolve operands** — look up SSA variables in state, or create constants.
   The `OperandResolver` mixin handles this with a resolution chain:
   `Constant → StateVariable → TopLevelVariable → ReferenceVariable`.

2. **Width-match** — if operands have different bit widths, extend the
   narrower one (zero-extend for unsigned, sign-extend for signed).

3. **Compute result** — apply the Z3 bitvector operation.

4. **Assert constraint** — `solver.assert_constraint(result == computed)`.
   This is **permanent** in the solver.

5. **Compute overflow predicates** — for arithmetic operations, create Z3
   predicates like `BVAddNoOverflow(a, b, signed)`.

6. **Add path constraints** — in checked contexts (not `unchecked {}`),
   add overflow safety constraints to the domain state.

7. **Store variable** in `State._variables` with metadata.

**Concrete example.** For `uint256 b = a + 1` in checked context:

```
Z3 assertions:
  TMP_0 == a_1 + 1         (binary addition)
  b_1 == TMP_0              (assignment)

Overflow predicate on TMP_0:
  no_overflow = Extract(256, 256, ZeroExt(1, a_1) + ZeroExt(1, 1)) == 0

Path constraint:
  UGE(a_1 + 1, a_1)        (checked: result >= input)
```

**For `require(x < 100)`:**

```
Z3 assertions:
  TMP_0 == If(ULT(x_1, 100), 1, 0)   (comparison)
  1 == TMP_0                            (require: condition must be true)
```

The require handler also checks satisfiability — if UNSAT, the domain
becomes BOTTOM (unreachable code).

### 3.4 Control Flow Joins

The `IntervalDomain` is a three-valued lattice:

```
BOTTOM (⊥) — unreachable
STATE      — concrete tracked state
TOP (⊤)    — unknown/unconstrained
```

Join rules:

```
BOTTOM ⊔ X      → X         (unreachable contributes nothing)
X      ⊔ BOTTOM → X         (no change)
STATE  ⊔ STATE  → merge variable dicts (union keys, keep self's versions)
X      ⊔ TOP    → TOP
TOP    ⊔ X      → TOP
```

For conditional branches, `apply_condition()` narrows the domain before
propagation:

- **Then-branch**: adds the comparison term as a path constraint
- **Else-branch**: adds `Not(comparison_term)` as a path constraint

Path constraints are stored per-state (not in the solver) and passed to
range queries at annotation time.

### 3.5 Loops and Widening

At loop headers (IFLOOP nodes), phi operations merge incoming values:

```
Phi: i_2 := ϕ(i_1, i_3)    // from init and back edge
Phi: sum_2 := ϕ(sum_1, sum_3)
```

The phi handler creates **unconstrained** variables at loop headers because
Z3 constraints are permanent — adding bounds at a loop header would make the
exit path unsatisfiable.

Widening in `apply_widening()` on back edges:

1. If previous state is BOTTOM (first iteration): no widening
2. Match variables by base name (strip SSA suffixes)
3. For each variable:
   - Query current and previous bounds via `solver.solve_range()`
   - If current ⊆ previous: keep (stable, converged)
   - If bounds grew: replace with unconstrained variable (full type range)

**This is why loop variables lose precision.** For
`for (uint i = 0; i < 10; i++)`, the variable `i` is reported as
`[0, type(uint256).max]` instead of `[0, 10]`. The widening detects that
`i`'s upper bound grows between iterations and widens to unconstrained.

The legacy analysis had threshold-based widening (widen to the next known
constant rather than to unconstrained), but this was removed in v2.

### 3.6 Post-State Contents

After analysis, each node has an `AnalysisState` with `pre` and `post`
`IntervalDomain`. A STATE domain contains:

| Field | Type | Purpose |
|-------|------|---------|
| `_variables` | `dict[str, TrackedSMTVariable]` | All variables in scope |
| `_comparisons` | `dict[str, ComparisonInfo]` | Condition terms for narrowing |
| `_path_constraints` | `list[SMTTerm]` | Branch-specific constraints |
| `_dependencies` | `dict[str, set[str]]` | Data dependency graph |
| `_storage_slots` | `dict[str, list[str]]` | Storage write tracking |
| `_used_variables` | `set[str]` | Variables referenced in ops |

Each `TrackedSMTVariable` wraps:
- `base: SMTVariable` — Z3 bitvector term + metadata (is_signed, bit_width)
- `no_overflow: Optional[SMTTerm]` — overflow predicate
- `no_underflow: Optional[SMTTerm]` — underflow predicate
- `is_unchecked: bool` — from `unchecked {}` context

The Z3 solver itself holds **all assertions from all nodes** (permanent).
The post-state's `_path_constraints` are the per-path refinements.

### 3.7 Annotation Phase (Range Solving)

After the analysis engine reaches fixpoint, the annotation phase queries Z3
for each variable's concrete [min, max] range.

For each variable at each node's post-state:

1. `_create_annotation()` builds a `RangeQueryConfig` with the node's
   path constraints and the `--timeout` value.

2. `solve_variable_range()` (in `analysis.py`):
   a. Check cache (LRU, 1000 entries, keyed by variable + constraints)
   b. Verify term is bitvector
   c. Extract metadata (is_signed, bit_width)
   d. Call `solver.solve_range(term, path_constraints, timeout_ms, signed)`

3. `Z3Solver.solve_range()`:
   a. **UNSAT pre-check**: create fresh `Solver()`, copy all assertions +
      path constraints, check with 100ms cap. If UNSAT → unreachable path.
   b. **Minimize**: create fresh `Optimize()`, copy everything, call
      `optimizer.minimize(objective)`, extract model value.
   c. **Maximize**: create another fresh `Optimize()`, same process with
      `optimizer.maximize(objective)`.
   d. For signed types: XOR with sign bit mask to convert signed ordering
      to unsigned (Z3 Optimize works on unsigned bitvectors).

4. If Z3 returns non-SAT (timeout/unknown):
   `_solve_and_cache()` falls back to `_get_fallback_range()` which returns
   full type bounds: `[0, 2^256-1]` for uint256, `[-2^255, 2^255-1]` for
   int256.

5. Overflow checking (deferred): tests satisfiability of `Not(no_overflow)`
   with timeout. If SAT, the variable can overflow.

**Resource cost per variable: 3 fresh Z3 instances** (1 Solver for UNSAT
pre-check + 2 Optimize for min/max), each copying all solver assertions.
No clause sharing between min and max queries.

### 3.8 Output

Two modes:
- **Human**: Rich terminal output with annotated source, tree-style range
  decorations, color-coded overflow warnings.
- **JSON**: Structured dict with functions, lines, annotations, errors.
  Used for testing and programmatic consumption.

---

## 4. The Z3 Constraint Model

### 4.1 What's in the Solver

All constraints are 256-bit bitvector arithmetic (matching Solidity's
uint256/int256). The solver accumulates:

- **Equality constraints**: `result == operation(operands)` for every
  assignment, binary op, comparison
- **Require/assert constraints**: `condition == 1`
- **Division safety**: `divisor != 0`
- **Checked overflow constraints**: `result >= left` for unsigned add, etc.
  (these go into path constraints, not the solver directly)

### 4.2 Concrete Example

For:

```solidity
function g(uint256 x) public pure returns (uint256) {
    require(x < 100);
    uint256 y = x + 10;
    return y;
}
```

**Solver assertions (permanent):**

```
[0] TMP_0 == If(ULT(x_1, 100), 1, 0)    // comparison
[1] 1 == TMP_0                            // require
[2] TMP_2 == x_1 + 10                     // addition
[3] y_1 == TMP_2                          // assignment
```

**Path constraints (per-state, at return node):**

```
UGE(x_1 + 10, x_1)     // checked add didn't wrap
```

**Range query results at return:**

```
x_1:   [0, 99]      // constrained by require
TMP_0: [1, 1]       // always true (require passed)
TMP_2: [10, 109]    // x in [0,99], so x+10 in [10,109]
y_1:   [10, 109]    // equals TMP_2
```

### 4.3 Loop Example

For:

```solidity
function loop(uint256 n) public pure returns (uint256) {
    require(n <= 10);
    uint256 sum = 0;
    for (uint256 i = 0; i < n; i++) {
        sum += i;
    }
    return sum;
}
```

**Solver assertions:**

```
[0] TMP_0 == If(ULE(n_1, 10), 1, 0)
[1] 1 == TMP_0
[2] 0 == sum_1
[3] 0 == i_1
[4] TMP_2 == If(ULT(i_2, n_1), 1, 0)
[5] sum_3 == sum_2 + i_2
[6] TMP_3 == i_2
[7] i_3 == i_2 + 1
[8] TMP_2 == If(ULT(i_2, n_1), 1, 0)    // duplicated from re-iteration
[9] sum_3 == sum_2 + i_2                  // duplicated from re-iteration
```

**Range results at return (14 iterations, 4.87s):**

```
n_1:   [0, 10]              // constrained by require
sum_1: [0, 0]               // initial value
i_1:   [0, 0]               // initial value
sum_2: [0, 2^256-1]         // WIDENED — loop variable
i_2:   [0, 2^256-1]         // WIDENED — loop variable
```

The phi variables `sum_2` and `i_2` are unconstrained because widening
detected bound growth and replaced them with fresh unconstrained variables.

---

## 5. SMT Solver Layer

### 5.1 Abstract Interface (`solver.py`)

`SMTSolver` is an abstract class with 50+ methods:
- Variable management: `declare_const`, `get_or_declare_const`, `create_constant`
- Constraint management: `assert_constraint`, `check_sat`, `push`/`pop`, `reset`
- 22 bitvector operations: `bv_add`, `bv_mul`, `bv_udiv`, `bv_slt`, etc.
- 8 overflow predicates: `bv_add_no_overflow`, `bv_mul_no_underflow`, etc.
- Boolean logic: `bool_and`, `bool_or`, `bool_not`, `ite`
- Range solving: `solve_range`

### 5.2 Z3 Implementation (`strategies/z3_solver.py`)

`Z3Solver(SMTSolver)` — 757 lines, the only concrete strategy.

**Initialization:**
- `use_optimizer=True` → creates `z3.Optimize()` (needed for min/max)
- `use_optimizer=False` → creates `z3.Solver()` with 5000ms timeout
- Tracks: constraint count, check count, total check time

**Variable storage:** `_variables: dict[str, SMTVariable]`.
All variables are 256-bit bitvectors (`z3.BitVec(name, 256)`) except
booleans. Variables are permanent once declared.

**Constraint dumping:** If `DUMP_CONSTRAINTS` env var is set, all assertions
are written to `/tmp/z3_constraints.smt2` in SMT-LIB format.

**Signed optimization trick:** Z3 Optimize works on unsigned bitvectors.
For signed types, `_prepare_objective_term()` XORs with the sign bit mask
(`term ^ (1 << (width-1))`) to convert signed ordering to unsigned, then
decodes the result back.

### 5.3 Cache (`cache.py`)

`RangeQueryCache` — LRU cache (OrderedDict, default 1000 entries).
Key: `(var_id_string, tuple_of_constraint_strings)`.
Avoids re-querying the same variable under the same constraints at different
nodes.

### 5.4 Telemetry (`telemetry.py`)

771 lines of instrumentation tracking:
- Per-solver: queries, SAT/UNSAT/timeout breakdown, timing
- Per-function: CFG nodes, loops, external calls, iterations, analysis time
- Precision: constant/boolean/full-range annotation counts, overflow counts
- Output: Rich table or plaintext summary, JSON metrics file

---

## 6. Operation Handlers (Detail)

### 6.1 Arithmetic (`binary/arithmetic.py`)

Handles `+`, `-`, `*`, `/`, `%`, `**`, `<<`, `>>`.

For each operation:
1. Resolve left/right operands
2. Width-match (extend narrower operand)
3. Determine signedness (signed if EITHER operand signed)
4. Compute Z3 result:
   - `+` → `bv_add`, `-` → `bv_sub`, `*` → `bv_mul`
   - `/` → `bv_sdiv`/`bv_udiv`, `%` → `bv_srem`/`bv_urem`
   - `**` → iterative squaring (constant exponents ≤ 256)
   - `<<` → `bv_shl`, `>>` → `bv_ashr`(signed)/`bv_lshr`(unsigned)
5. Assert `result == computed`
6. Compute overflow predicates:
   - Add: `BVAddNoOverflow`, `BVAddNoUnderflow`
   - Sub: `BVSubNoUnderflow`, `BVSubNoOverflow`
   - Mul: `BVMulNoOverflow`, `BVMulNoUnderflow`
   - Div: `BVSDivNoOverflow` (signed only, for INT_MIN / -1)
7. In checked context: add path constraints
   - Unsigned add: `UGE(result, left)` (no wraparound)
   - Unsigned sub: `ULE(result, left)` (no wraparound)
   - Division/modulo: `divisor != 0`

### 6.2 Comparison (`binary/comparison.py`)

Handles `<`, `>`, `<=`, `>=`, `==`, `!=`.

Creates a comparison term, then wraps in ITE to 256-bit:
`result = If(comparison, 1, 0)`.

Stores `ComparisonInfo(comparison_term)` in state for condition narrowing.

### 6.3 Phi (`phi.py`)

At loop headers (IFLOOP): creates unconstrained variable.
At other joins: creates disjunction `result == v1 OR result == v2 OR ...`
if all incoming values are tracked. If any are untracked, creates
unconstrained.

### 6.4 Require/Assert (`solidity_call/require_assert.py`)

Asserts `condition == 1`. Checks satisfiability — if UNSAT, sets domain
to BOTTOM (dead code elimination).

### 6.5 Type Conversion (`type_conversion.py`)

Widening: zero-extend or sign-extend.
Narrowing: extract low bits.
Same width signed ↔ unsigned: reinterpret (same bits, different metadata).

### 6.6 Interprocedural (`interprocedural.py`)

Handles `InternalCall` and `LibraryCall`:
- If callee is "simple" (≤ 20 nodes, no loops): analyze it with a
  separate solver, map return values back.
- Otherwise: create unconstrained results.
- No context sensitivity, no recursion support.

### 6.7 Unconstrained Handlers

These create unconstrained variables (full type range):
- `IndexHandler` — array/mapping reads
- `LengthHandler` — `.length` access
- `MemberHandler` — struct field access
- `LowLevelCallHandler` — `.call()`, `.delegatecall()`
- `InternalDynamicCallHandler` — function pointers
- `UnpackHandler` — tuple unpacking
- `InitArrayHandler` — array initialization
- `GasleftHandler` — `gasleft()`
- `SloadHandler` — storage reads
- `AbiHandler` — `abi.encode`/`abi.decode`

---

## 7. Known Limitations

From `LIMITATIONS.md` and code analysis:

1. **Loop variables** are widened to full type range. A `for (uint i = 0;
   i < 10; i++)` reports `i ∈ [0, 2^256-1]`.

2. **Storage variables** (SLOAD) are unconstrained. No cross-function or
   cross-transaction state tracking.

3. **External calls** return unconstrained values. No inter-contract
   analysis.

4. **Array/mapping contents** are unconstrained. Only the container
   variable itself is tracked.

5. **Assembly/Yul** (mload, mstore) raises `NotImplementedError`.

6. **Complex interprocedural**: only simple callees (≤ 20 nodes, no loops)
   are inlined. Recursive calls produce unconstrained results.

7. **Struct fields** are unconstrained on access.

8. **Dynamic dispatch** (function pointers) produces unconstrained results.

9. **256-bit multiplication** is exponentially expensive for Z3 Optimize.
   Queries frequently timeout, producing full-range fallback bounds.

10. **Widening is imprecise**: the current strategy widens to unconstrained
    on any bound growth. Threshold-based narrowing was available in legacy
    but removed in v2.

11. **Constraint duplication**: re-iteration of loop bodies adds duplicate
    assertions to the solver (observed in loop example: assertions [4]=[8],
    [5]=[9]).

12. **No push/pop during analysis**: constraints are permanent. The solver
    grows monotonically. For large functions this can slow down Z3.

---

## 8. Performance Characteristics

### 8.1 Timeout Architecture

Single `--timeout` flag (default 3000ms) flows through both phases:
- **Analysis phase**: `IntervalAnalysis._timeout_ms` → used in widening
  queries (`_query_variable_bounds` → `solver.solve_range`)
- **Annotation phase**: `_create_annotation` → `solve_variable_range` →
  `solver.solve_range`

Below 500ms, Z3 Optimize on 256-bit bitvectors reliably returns unknown,
which silently degrades to full type-range bounds. A warning is emitted
at startup for `--timeout < 500`.

### 8.2 Cost Per Variable

Each variable's range query creates:
- 1 fresh `z3.Solver()` for UNSAT pre-check (100ms cap)
- 1 fresh `z3.Optimize()` for minimize
- 1 fresh `z3.Optimize()` for maximize

All three copy every assertion from the main solver. No clause sharing
between min and max queries. For a function with N assertions and V
variables, annotation costs O(V × N) assertion copies.

### 8.3 Empirical Observations

From Test_Mul.sol (33 queries, 3000ms timeout):
- p50: 64ms, p95: 6695ms, p99: 6756ms
- 0% fallback rate
- Bimodal distribution: fast queries (comparisons, simple arithmetic)
  resolve in <100ms; multiplication/division queries take seconds

### 8.4 Caching

`RangeQueryCache` (LRU, 1000 entries) deduplicates queries for the same
variable under the same constraints across different nodes. Key includes
variable ID string and all constraint strings.

---

## 9. Evolution from Legacy (v1 → v2)

### Removed in v2
- Memory safety analysis (`safety/memory_safety.py`) — separate concern
- mload/mstore handlers — assembly not supported
- Calldata tracking — calldata variables unconstrained
- EVM builtins (block.timestamp, msg.sender, gas) — now unconstrained
- Struct constructors (`new_elementary_type`, `new_structure`)
- Threshold-based widening — simplified to unconstrained

### Added in v2
- Telemetry system (771 lines of instrumentation)
- Range query caching (LRU)
- JSON output mode for testing/tooling
- Overflow predicate tracking with deferred checking
- Cleaner operation handler architecture
- Rich terminal rendering

### Simplified in v2
- Widening: unconstrained instead of threshold-based
- State: removed memory tracking, calldata tracking
- Solidity calls: fewer specialized handlers, more unconstrained fallbacks

---

## 10. Logger

`DataFlowLogger` is a singleton (via `get_logger()`) wrapping loguru.
Provides standard logging methods plus:
- `error_and_raise(message, exception_class)` — log + raise in one call
- Error collection mode for JSON output: `start_collecting_errors()` /
  `get_collected_errors()` embeds warnings/errors in JSON response

---

## 11. Open Questions and Future Work

1. **Loop precision**: threshold-based widening could recover loop variable
   ranges. The infrastructure exists (thresholds are collected in
   `prepare_for_function`), but the actual threshold-narrowing path is not
   used — `apply_widening` always widens to unconstrained on growth.

2. **Constraint duplication**: loop re-iteration adds duplicate assertions.
   Could use push/pop or deduplication to keep the solver lean.

3. **Per-variable timeout adaptation**: multiplication queries dominate
   wall time. Could skip Z3 Optimize for known-expensive patterns (256-bit
   mul/div) and go straight to fallback bounds.

4. **Storage tracking**: cross-function state variable analysis would
   recover precision for storage reads. The legacy code had some of this.

5. **Memory safety**: the legacy analysis had buffer overflow detection.
   Could be re-added as a separate pass consuming interval results.

6. **Default timeout**: 3000ms was calibrated with `--budget` as a
   backstop. Empirical data suggests most queries resolve in <100ms,
   with outliers (multiplication) taking 3-7 seconds regardless.
   A lower default (e.g., 1000ms) might be appropriate with explicit
   opt-in for expensive queries.
