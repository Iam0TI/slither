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
from slither.analyses.data_flow.analyses.interval.core.tracked_variable import (
    TrackedSMTVariable,
)

if TYPE_CHECKING:
    from slither.analyses.data_flow.analyses.interval.analysis.domain import (
        IntervalDomain,
    )
    from slither.core.cfg.node import Node


def _base_field_name(variable: Variable, field: str) -> str:
    """Build a storage-stable field key from a variable's base name.

    Uses ``Variable.name`` (no SSA suffix) so that writes and reads
    across SSA versions of the same struct resolve to one key.
    """
    base = variable.name if variable.name is not None else str(variable)
    return f"{base}.{field}"


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
            operation, tracked_reference, field_name, field_type, domain
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

    def _resolve_storage_alias(
        self,
        operation: Member,
        field_name: str,
        domain: "IntervalDomain",
    ) -> TrackedSMTVariable | None:
        """Find a prior write to the same storage field across SSA versions.

        When the SSA-versioned field name (e.g. ``feeData_3.curated``)
        is missing from state, falls back to looking up the base-name
        alias (``feeData.curated``) which is registered on every access.
        """
        points_to_target = operation.lvalue.points_to
        if not isinstance(points_to_target, Variable):
            return None

        field_identifier = operation.variable_right.value
        alias = _base_field_name(points_to_target, field_identifier)
        if alias == field_name:
            return None
        return domain.state.get_variable(alias)

    def _link_reference_to_field(
        self,
        operation: Member,
        tracked_reference: TrackedSMTVariable,
        field_name: str,
        field_type: ElementaryType,
        domain: "IntervalDomain",
    ) -> None:
        """Link reference variable to struct field with equality constraint.

        Looks up the field by its SSA-versioned name first.  If not
        found, falls back to a storage-stable base-name alias that
        survives SSA version bumps on the parent struct.  After
        linking, registers the field under the base-name alias so
        future reads can find it.
        """
        tracked_field = domain.state.get_variable(field_name)

        if tracked_field is None:
            tracked_field = self._resolve_storage_alias(operation, field_name, domain)

        if tracked_field is None:
            tracked_field = self._create_tracked_variable(
                field_name, field_type, domain
            )

        field_term = match_width(
            self.solver, tracked_field.term, tracked_reference.term
        )
        self.solver.assert_constraint(tracked_reference.term == field_term)

        # Register under base-name alias for cross-version lookups
        self._register_storage_alias(operation, tracked_field, domain)

    def _register_storage_alias(
        self,
        operation: Member,
        tracked_field: TrackedSMTVariable,
        domain: "IntervalDomain",
    ) -> None:
        """Register field under its base-name alias for cross-SSA lookups."""
        points_to_target = operation.lvalue.points_to
        if not isinstance(points_to_target, Variable):
            return

        field_identifier = operation.variable_right.value
        alias = _base_field_name(points_to_target, field_identifier)
        domain.state.set_variable(alias, tracked_field)
