"""Fixable repairs for ElOverblik (DK)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir

from .helpers import async_deselect_points, selected_points


class DuplicateMeteringPointFlow(RepairsFlow):
    """Deselect a metering point that reports the same values as another."""

    def __init__(self, data: dict[str, Any] | None) -> None:
        """Remember which entry and which point the issue is about."""
        self._data = dict(data or {})

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Go straight to the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask once, then remove the point from the entry's selection."""
        entry = self.hass.config_entries.async_get_entry(
            str(self._data.get("entry_id") or "")
        )
        remove = str(self._data.get("remove") or "")
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        selected = {point.metering_point_id for point in selected_points(entry)}
        if remove not in selected:
            return self.async_abort(reason="already_deselected")
        if user_input is not None:
            if not async_deselect_points(self.hass, entry, {remove}):
                return self.async_abort(reason="last_point")
            return self.async_create_entry(data={})
        issue = ir.async_get(self.hass).async_get_issue(self.handler, self.issue_id)
        placeholders = dict(issue.translation_placeholders or {}) if issue else {}
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow for an ElOverblik repair issue."""
    del hass, issue_id
    return DuplicateMeteringPointFlow(data)
