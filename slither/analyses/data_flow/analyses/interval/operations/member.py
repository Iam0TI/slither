"""Member operation handler for interval analysis."""

from __future__ import annotations

from typing import TYPE_CHECKING

from slither.core.solidity_types.elementary_type import ElementaryType
from slither.core.variables.variable import Variable
from slither.slithir.operations.member import Member

from slither.analyses.data_flow.analyses.interval.operations.base import (
    BaseOperationHandler,
)
from slither.analyses.data_flow.analyses.interval.operations.type_utils import (
    get_variable_name,
    get_bit_width,
    is_signed_type,
    type_to_sort,
)
from slither.analyses.data_flow.analyses.interval.operations.type_conversion import (
    match_width,
)
from slither.analyses.data_flow.analyses.interval.core.state import State
from slither.analyses.data_flow.analyses.interval.core.tracked_variable import (
    TrackedSMTVariable,
)

if TYPE_CHECKING:
    from slither.analyses.data_flow.analyses.interval.analysis.domain import (
        IntervalDomain,
    )
    from slither.core.cfg.node import Node


class MemberHandler(BaseOperationHandler):
    """Handler for Member operations (struct field access)."""

    def handle(
        self,
        operation: Member,
        domain: "IntervalDomain",
        node: "Node",
    ) -> None:
        """Process struct field access operation."""
        field_type = self._get_field_type(operation)
        if field_type is None:
            return

        reference_name = get_variable_name(operation.lvalue)
        field_name = self._build_field_name(operation)

        tracked_reference = self._create_tracked_variable(
            reference_name, field_type, domain
        )
        self._link_reference_to_field(
            tracked_reference, field_name, field_type, domain, operation
        )

    def _get_field_type(self, operation: Member) -> ElementaryType | None:
        """Extract elementary type from the struct field."""
        lvalue_type = operation.lvalue.type
        if isinstance(lvalue_type, ElementaryType):
            return lvalue_type
        return None

    def _build_field_name(self, operation: Member) -> str:
        """Build field name using points_to for write-through semantics."""
        points_to_target = operation.lvalue.points_to
        if isinstance(points_to_target, Variable):
            struct_name = get_variable_name(points_to_target)
        else:
            struct_name = get_variable_name(operation.variable_left)

        field_identifier = operation.variable_right.value
        return f"{struct_name}.{field_identifier}"

    def _create_tracked_variable(
        self,
        name: str,
        element_type: ElementaryType,
        domain: "IntervalDomain",
    ) -> TrackedSMTVariable:
        """Create and register a tracked SMT variable."""
        sort = type_to_sort(element_type)
        signed = is_signed_type(element_type)
        bit_width = get_bit_width(element_type)

        tracked = TrackedSMTVariable.create(
            self.solver, name, sort, is_signed=signed, bit_width=bit_width
        )
        domain.state.set_variable(name, tracked)
        return tracked

    def _find_latest_field(
        self,
        operation: Member,
        field_name: str,
        domain: "IntervalDomain",
    ) -> TrackedSMTVariable | None:
        """Find the latest field variable across SSA versions.

        When the SSA-versioned lookup misses (e.g. feeData_3.curated
        not in state), scans for the last-inserted variable whose name
        ends with the same field suffix and whose struct part shares
        the same base name.  Dict insertion order (Python 3.7+)
        ensures the last match is the most recent write.
        """
        points_to_target = operation.lvalue.points_to
        if not isinstance(points_to_target, Variable):
            return None

        base_name = points_to_target.name
        if base_name is None:
            return None

        field_suffix = f".{operation.variable_right.value}"

        state = domain.state
        if isinstance(state, State):
            range_vars = state.get_range_variables()
        else:
            return None

        latest = None
        for variable_name in range_vars:
            if variable_name == field_name:
                continue
            if not variable_name.endswith(field_suffix):
                continue
            if not variable_name.startswith(base_name):
                continue
            latest = range_vars[variable_name]
        return latest

    def _link_reference_to_field(
        self,
        tracked_reference: TrackedSMTVariable,
        field_name: str,
        field_type: ElementaryType,
        domain: "IntervalDomain",
        operation: Member | None = None,
    ) -> None:
        """Link reference variable to struct field with equality constraint.

        Looks up the field by its SSA-versioned name first.  If not
        found, scans state for the latest field variable with the same
        base struct name and field suffix.
        """
        tracked_field = domain.state.get_variable(field_name)

        if tracked_field is None and operation is not None:
            tracked_field = self._find_latest_field(operation, field_name, domain)

        if tracked_field is None:
            tracked_field = self._create_tracked_variable(
                field_name, field_type, domain
            )

        field_term = match_width(
            self.solver, tracked_field.term, tracked_reference.term
        )
        self.solver.assert_constraint(tracked_reference.term == field_term)
