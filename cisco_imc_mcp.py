"""Cisco IMC MCP server.

Stable XML API (Nuova) calls for inventory/health/power management.
Avoids Redfish because the C220 M4 returns 503s on several Redfish
endpoints.

Read by default. Set ALLOW_WRITE=true in .env to enable power tools.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field


# ----- env -----

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

IMC_HOST = os.getenv("IMC_HOST", "")
IMC_USERNAME = os.getenv("IMC_USERNAME", "")
IMC_PASSWORD = os.getenv("IMC_PASSWORD", "")
IMC_VERIFY_SSL = os.getenv("IMC_VERIFY_SSL", "false").lower() == "true"
IMC_TIMEOUT = float(os.getenv("IMC_TIMEOUT", "30"))
RACK_DN = os.getenv("IMC_RACK_DN", "sys/rack-unit-1")
ALLOW_WRITE = os.getenv("ALLOW_WRITE", "false").lower() == "true"


# ----- shared -----

mcp = FastMCP("cisco-imc")


def _require_config() -> Optional[str]:
    missing = [
        name for name, val in (
            ("IMC_HOST", IMC_HOST),
            ("IMC_USERNAME", IMC_USERNAME),
            ("IMC_PASSWORD", IMC_PASSWORD),
        )
        if not val
    ]
    if missing:
        return (
            "ERROR: missing environment variables: "
            f"{', '.join(missing)}. Check .env in {BASE_DIR}."
        )
    return None


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


async def _post_xml(client: httpx.AsyncClient, body: str) -> ET.Element:
    url = f"https://{IMC_HOST}/nuova"
    headers = {"Content-Type": "application/xml"}
    resp = await client.post(url, content=body, headers=headers)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    err = root.attrib.get("errorCode")
    if err and err != "0":
        msg = root.attrib.get("errorDescr", "unknown error")
        raise RuntimeError(f"IMC API error {err}: {msg}")
    return root


@contextlib.asynccontextmanager
async def _imc_session() -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Login, yield (client, cookie), logout on exit.

    httpx is used because requests is sync; everything else in the MCP
    server is async too.
    """
    async with httpx.AsyncClient(
        verify=IMC_VERIFY_SSL,
        timeout=IMC_TIMEOUT,
    ) as client:
        login_body = (
            f'<aaaLogin inName="{_xml_escape(IMC_USERNAME)}" '
            f'inPassword="{_xml_escape(IMC_PASSWORD)}"/>'
        )
        root = await _post_xml(client, login_body)
        cookie = root.attrib.get("outCookie")
        if not cookie:
            raise RuntimeError("Login failed: no outCookie returned")
        try:
            yield client, cookie
        finally:
            with contextlib.suppress(Exception):
                logout_body = f'<aaaLogout inCookie="{cookie}"/>'
                await _post_xml(client, logout_body)


def _format_http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return (
            f"HTTP error: {exc.response.status_code} "
            f"on {exc.request.url}: {exc.response.text[:200]}"
        )
    if isinstance(exc, httpx.RequestError):
        return f"Network error: {exc!r}"
    return f"HTTP error: {exc}"


async def _resolve_dn(
    client: httpx.AsyncClient, cookie: str, dn: str
) -> Optional[ET.Element]:
    body = (
        f'<configResolveDn cookie="{cookie}" inHierarchical="false" '
        f'dn="{_xml_escape(dn)}"/>'
    )
    root = await _post_xml(client, body)
    out = root.find("outConfig")
    if out is None or len(out) == 0:
        return None
    return out[0]



async def _resolve_class(
    client: httpx.AsyncClient, cookie: str, class_id: str
) -> list[ET.Element]:
    body = (
        f'<configResolveClass cookie="{cookie}" inHierarchical="false" '
        f'classId="{_xml_escape(class_id)}"/>'
    )
    root = await _post_xml(client, body)
    out = root.find("outConfigs")
    if out is None:
        return []
    return list(out)


async def _conf_mo(
    client: httpx.AsyncClient,
    cookie: str,
    dn: str,
    class_name: str,
    attrs: dict[str, str],
) -> ET.Element:
    attr_str = " ".join(f'{k}="{_xml_escape(v)}"' for k, v in attrs.items())
    body = (
        f'<configConfMo cookie="{cookie}" dn="{_xml_escape(dn)}">'
        f'<inConfig>'
        f'<{class_name} dn="{_xml_escape(dn)}" {attr_str}/>'
        f'</inConfig>'
        f'</configConfMo>'
    )
    return await _post_xml(client, body)


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FormatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


class ConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = Field(
        default=False,
        description=(
            "Must be true to execute. Only set after the user has explicitly "
            "asked for this action."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        description="Optional short note about why this action is being taken.",
        max_length=200,
    )


def _missing_confirm_error(action: str) -> str:
    return (
        f"ERROR: {action} requires confirm=true. This will affect the "
        "running server. Pass confirm=true after the user has explicitly "
        "asked to proceed."
    )


def _missing_data_loss_error(action: str) -> str:
    return (
        f"ERROR: {action} requires i_understand_data_loss=true. This "
        "operation is irreversible. Pass i_understand_data_loss=true "
        "only after the user has explicitly acknowledged the risk."
    )


class DiskSmartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    controller: Optional[str] = Field(
        default=None,
        description=(
            "Controller ID to scope the query to (e.g. 'SLOT-HBA'). "
            "If omitted, drives from all controllers are returned."
        ),
        max_length=64,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


class EventLogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_entries: int = Field(
        default=50,
        description="Maximum number of entries to return (most recent first).",
        ge=1,
        le=1000,
    )
    severity_filter: Optional[str] = Field(
        default=None,
        description=(
            "Only return entries with this severity (case-insensitive substring "
            "match against the entry's severity field, e.g. 'critical', "
            "'warning', 'info')."
        ),
        max_length=32,
        pattern=r"^[A-Za-z_-]+$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'.",
    )


class ClearLogInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = Field(
        default=False,
        description=(
            "Must be true to execute. Only set after the user has explicitly "
            "asked to clear the SEL."
        ),
    )
    i_understand_data_loss: bool = Field(
        default=False,
        description=(
            "Must be true. Clearing the SEL is irreversible \u2014 all event "
            "history (hardware fault events, BIOS messages, etc.) is "
            "permanently lost."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        description="Optional short note about why the SEL is being cleared.",
        max_length=200,
    )


def _attrs(el: ET.Element) -> dict[str, str]:
    return dict(el.attrib)


def _filter_attrs(el: ET.Element, keys: list[str]) -> dict[str, str]:
    a = el.attrib
    return {k: a[k] for k in keys if k in a}


def _to_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


# ----- read tools -----

@mcp.tool(
    name="imc_get_system_info",
    annotations={
        "title": "Get IMC System Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_get_system_info(params: FormatInput) -> str:
    """Get hardware summary of the rack-unit (model, serial, CPU, RAM, power state).

    Returns:
        str: Multi-line summary or JSON.
    """
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    try:
        async with _imc_session() as (client, cookie):
            rack = await _resolve_dn(client, cookie, RACK_DN)
    except Exception as exc:
        return _format_http_error(exc)
    if rack is None:
        return f"No data: rack-unit '{RACK_DN}' not found."

    if params.response_format == ResponseFormat.JSON:
        return _to_json(_attrs(rack))

    a = rack.attrib
    return (
        f"## System Info\n"
        f"- **Model**: {a.get('model', '?')}\n"
        f"- **Serial**: {a.get('serial', '?')}\n"
        f"- **Total Memory**: {a.get('totalMemory', '?')} MiB\n"
        f"- **CPUs**: {a.get('numOfCpus', '?')}\n"
        f"- **Cores**: {a.get('numOfCores', '?')}\n"
        f"- **Threads**: {a.get('numOfThreads', '?')}\n"
        f"- **Adaptors**: {a.get('numOfAdaptors', '?')}\n"
        f"- **Power State**: {a.get('operPower', '?')}\n"
        f"- **Presence**: {a.get('presence', '?')}\n"
        f"- **Avail. Memory**: {a.get('availableMemory', '?')} MiB\n"
    )



@mcp.tool(
    name="imc_get_power_status",
    annotations={
        "title": "Get Power Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_get_power_status(params: EmptyInput) -> str:
    """Return the current operational power state (on/off) of the server.

    Returns:
        str: 'Power state: on|off|...' or an error message.
    """
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    try:
        async with _imc_session() as (client, cookie):
            rack = await _resolve_dn(client, cookie, RACK_DN)
    except Exception as exc:
        return _format_http_error(exc)
    if rack is None:
        return f"No data: rack-unit '{RACK_DN}' not found."
    state = rack.attrib.get("operPower", "?")
    return f"Power state: {state}"



@mcp.tool(
    name="imc_get_health",
    annotations={
        "title": "Get IMC Health",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_get_health(params: FormatInput) -> str:
    """Get power supplies, fans, and CPU temperature sensor readings.

    Returns:
        str: Health summary with status of each component.
    """
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    try:
        async with _imc_session() as (client, cookie):
            psus = await _resolve_class(client, cookie, "equipmentPsu")
            fans = await _resolve_class(client, cookie, "equipmentFan")
            temps = await _resolve_class(client, cookie, "processorEnvStats")
    except Exception as exc:
        return _format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return _to_json({
            "psus": [_attrs(p) for p in psus],
            "fans": [_attrs(f) for f in fans],
            "temps": [_attrs(t) for t in temps],
        })

    lines = ["## Health"]

    lines.append("\n### Power Supplies")
    if not psus:
        lines.append("- _(none reported)_")
    for p in psus:
        a = p.attrib
        lines.append(
            f"- PSU {a.get('id', '?')} ({a.get('model', '?')}): "
            f"operability={a.get('operability', '?')}, "
            f"voltage={a.get('voltage', '?')}, "
            f"thermal={a.get('thermal', '?')}"
        )

    lines.append("\n### Fans")
    if not fans:
        lines.append("- _(none reported)_")
    for f in fans:
        a = f.attrib
        lines.append(
            f"- Fan {a.get('id', '?')}: "
            f"operability={a.get('operability', '?')}, "
            f"presence={a.get('presence', '?')}"
        )

    lines.append("\n### CPU Temperatures")
    if not temps:
        lines.append("- _(none reported)_")
    for t in temps:
        a = t.attrib
        # dn typically: sys/rack-unit-1/board/cpu-1/env-stats
        dn = a.get("dn", "")
        cpu_id = "?"
        for part in dn.split("/"):
            if part.startswith("cpu-"):
                cpu_id = part.split("-", 1)[-1]
                break
        lines.append(
            f"- CPU {cpu_id}: "
            f"temperature={a.get('temperature', '?')} C "
            f"(status={a.get('thresholdStatus', '?')})"
        )

    return "\n".join(lines)



@mcp.tool(
    name="imc_list_drives",
    annotations={
        "title": "List Physical Drives",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_list_drives(params: FormatInput) -> str:
    """List physical drives reported by the storage controllers.

    Returns:
        str: For each drive: slot, vendor, model, capacity, type, health.
    """
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    try:
        async with _imc_session() as (client, cookie):
            drives = await _resolve_class(client, cookie, "storageLocalDisk")
    except Exception as exc:
        return _format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return _to_json([_attrs(d) for d in drives])

    if not drives:
        return "_No physical drives reported by IMC._"

    lines = ["## Physical Drives", ""]
    lines.append(
        "| Slot | Vendor | Model | Size | Type | Status | Health |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for d in drives:
        a = d.attrib
        lines.append(
            f"| {a.get('id', '?')} | "
            f"{a.get('vendor', '?')} | "
            f"{a.get('productId', a.get('model', '?'))} | "
            f"{a.get('coercedSize', a.get('size', '?'))} | "
            f"{a.get('mediaType', '?')} | "
            f"{a.get('linkSpeed', a.get('operability', '?'))} | "
            f"{a.get('predictiveFailureCount', a.get('health', 'OK'))} |"
        )
    return "\n".join(lines)



# ----- power control tools -----

async def _power_action(action: str) -> str:
    """Send a power-mgmt admin action to the rack-unit's computeRackUnit MO.

    Action must be one of the valid Cisco IMC values for adminPower.
    """
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    if not ALLOW_WRITE:
        return (
            "ERROR: write actions are disabled. Set ALLOW_WRITE=true in .env "
            "to enable power management tools."
        )
    try:
        async with _imc_session() as (client, cookie):
            root = await _conf_mo(
                client, cookie, RACK_DN, "computeRackUnit",
                {"adminPower": action},
            )
    except Exception as exc:
        return _format_http_error(exc)
    return f"OK: adminPower={action} sent to {RACK_DN}.\n{ET.tostring(root, encoding='unicode')[:500]}"


@mcp.tool(
    name="imc_power_on",
    annotations={
        "title": "Power On Server",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_power_on(params: ConfirmInput) -> str:
    """Power on the server.

    Requires confirm=true.

    Sets adminPower=up on the rack-unit. Idempotent if already on.
    """
    if not params.confirm:
        return _missing_confirm_error("imc_power_on")
    return await _power_action("up")


@mcp.tool(
    name="imc_power_off_graceful",
    annotations={
        "title": "Graceful Shutdown",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_power_off_graceful(params: ConfirmInput) -> str:
    """Request a graceful OS shutdown via ACPI.

    Requires confirm=true.

    Sets adminPower=soft-shut-down. The OS must honor ACPI for this to
    succeed. If the OS is hung, use imc_power_off_force instead.
    """
    if not params.confirm:
        return _missing_confirm_error("imc_power_off_graceful")
    return await _power_action("soft-shut-down")


@mcp.tool(
    name="imc_power_off_force",
    annotations={
        "title": "Force Power Off (DESTRUCTIVE)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_power_off_force(params: ConfirmInput) -> str:
    """Immediately cut power.

    Requires confirm=true.

    Sets adminPower=down. Equivalent to yanking the power cable. Use
    only when the OS is hung or unresponsive; data loss is possible.
    """
    if not params.confirm:
        return _missing_confirm_error("imc_power_off_force")
    return await _power_action("down")


@mcp.tool(
    name="imc_reboot",
    annotations={
        "title": "Hard Reset (DESTRUCTIVE)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_reboot(params: ConfirmInput) -> str:
    """Force restart (hard reset) the server.

    Requires confirm=true.

    Sets adminPower=hard-reset-immediate. Equivalent to pressing the
    reset button. Data loss is possible; prefer imc_power_off_graceful
    + imc_power_on for normal restarts.
    """
    if not params.confirm:
        return _missing_confirm_error("imc_reboot")
    return await _power_action("hard-reset-immediate")


@mcp.tool(
    name="imc_power_cycle",
    annotations={
        "title": "Power Cycle (DESTRUCTIVE)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_power_cycle(params: ConfirmInput) -> str:
    """Power cycle the server (off then on).

    Requires confirm=true.

    Sets adminPower=cycle-immediate. Equivalent to pulling power and
    plugging it back in. Use to recover from rare firmware-level hangs.
    """
    if not params.confirm:
        return _missing_confirm_error("imc_power_cycle")
    return await _power_action("cycle-immediate")



# ----- detailed health tools -----

@mcp.tool(
    name="imc_get_psu_details",
    annotations={
        "title": "Get PSU Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_get_psu_details(params: FormatInput) -> str:
    """Get detailed PSU information including input/output power and operability.

    Reads the equipmentPsu MO directly — all PSU details (input voltage range,
    output voltage, max output wattage, operability, voltage/thermal/power
    status flags) live as native attributes on equipmentPsu itself. There is
    no separate equipmentPsuInputStats / equipmentPsuOutputStats class in
    the Cisco IMC XML schema; those names were incorrect in an earlier
    version of this tool. Useful for confirming the PSU 1 warning seen
    historically on this server.

    Returns:
        str: Per-PSU details in markdown or JSON.
    """
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    try:
        async with _imc_session() as (client, cookie):
            psus = await _resolve_class(client, cookie, "equipmentPsu")
    except Exception as exc:
        return _format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return _to_json([_attrs(p) for p in psus])

    if not psus:
        return "_No PSUs reported by IMC (server may be powered off)._"

    lines = ["## Power Supply Details", ""]
    for p in psus:
        a = p.attrib
        lines.append(f"### PSU {a.get('id', '?')}")
        lines.append(f"- **Model**: {a.get('model', '?')}")
        if a.get("pid"):
            lines.append(f"- **PID**: {a.get('pid')}")
        if a.get("vendor"):
            lines.append(f"- **Vendor**: {a.get('vendor')}")
        if a.get("serial"):
            lines.append(f"- **Serial**: {a.get('serial')}")
        if a.get("fwVersion"):
            lines.append(f"- **Firmware**: {a.get('fwVersion')}")
        lines.append(f"- **Presence**: {a.get('presence', '?')}")
        lines.append(f"- **Operability**: {a.get('operability', '?')}")
        if a.get("power"):
            lines.append(f"- **Power state**: {a.get('power')}")
        if a.get("thermal"):
            lines.append(f"- **Thermal**: {a.get('thermal')}")
        if a.get("voltage"):
            lines.append(f"- **Voltage status**: {a.get('voltage')}")
        if a.get("input"):
            lines.append(f"- **Input**: {a.get('input')}")
        if a.get("output"):
            lines.append(f"- **Output**: {a.get('output')}")
        if a.get("maxOutput"):
            lines.append(f"- **Max Output**: {a.get('maxOutput')}")
        lines.append("")
    return "\n".join(lines).rstrip()



@mcp.tool(
    name="imc_get_disk_smart",
    annotations={
        "title": "Get Disk SMART Data",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_get_disk_smart(params: DiskSmartInput) -> str:
    """Get physical drive health and error counters from the storage controller.

    Returns SMART-like data per drive (link errors, media errors, predictive
    failure count, power-on hours). Useful for spotting silently failing
    drives before they show in a RAID alert.

    Returns:
        str: Per-drive health in markdown or JSON.
    """
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    try:
        async with _imc_session() as (client, cookie):
            drives = await _resolve_class(client, cookie, "storageLocalDisk")
    except Exception as exc:
        return _format_http_error(exc)

    if params.controller:
        ctrl = params.controller.lower()
        drives = [
            d for d in drives
            if ctrl in (d.attrib.get("dn") or "").lower()
        ]

    if params.response_format == ResponseFormat.JSON:
        return _to_json([_attrs(d) for d in drives])

    if not drives:
        msg = "_No drives reported by IMC"
        if params.controller:
            msg += f" for controller '{params.controller}'"
        msg += "._"
        return msg

    lines = ["## Drive Health (SMART)", ""]
    lines.append(
        "| Slot | Model | Size | Type | Link Errors | "
        "Media Errors | Pred. Fail | Power-on Hrs | Health |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for d in drives:
        a = d.attrib
        media_err = a.get("mediaErrorCount", a.get("smartMediaErrors", "0"))
        link_err = a.get("linkErrorCount", a.get("smartLinkErrors", "0"))
        pred = a.get("predictiveFailureCount", "0")
        hrs = a.get("powerOnHours", a.get("powerOnTime", "?"))
        health = a.get("health", a.get("operability", "?"))
        lines.append(
            f"| {a.get('id', '?')} | "
            f"{a.get('productId', a.get('model', '?'))} | "
            f"{a.get('coercedSize', a.get('size', '?'))} MB | "
            f"{a.get('mediaType', '?')} | "
            f"{link_err} | "
            f"{media_err} | "
            f"{pred} | "
            f"{hrs} | "
            f"{health} |"
        )
    return "\n".join(lines)



@mcp.tool(
    name="imc_get_memory_health",
    annotations={
        "title": "Get Memory Health",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_get_memory_health(params: FormatInput) -> str:
    """Get per-DIMM health, capacity, speed, vendor, and presence.

    Reports both populated and empty slots so missing/failed DIMMs are
    obvious. Useful for tracking down ECC errors or planning a memory
    upgrade.

    Returns:
        str: Per-DIMM details in markdown or JSON.
    """
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    try:
        async with _imc_session() as (client, cookie):
            arrays = await _resolve_class(client, cookie, "memoryArray")
            units = await _resolve_class(client, cookie, "memoryUnit")
    except Exception as exc:
        return _format_http_error(exc)

    if params.response_format == ResponseFormat.JSON:
        return _to_json({
            "arrays": [_attrs(a) for a in arrays],
            "units": [_attrs(u) for u in units],
        })

    lines = ["## Memory Health", ""]

    if arrays:
        lines.append("### Memory Arrays")
        for arr in arrays:
            a = arr.attrib
            lines.append(
                f"- Array {a.get('id', '?')}: "
                f"populated={a.get('populated', '?')}, "
                f"current_capacity={a.get('currCapacity', '?')} MB, "
                f"max_capacity={a.get('maxCapacity', '?')} MB"
            )
        lines.append("")

    populated = [u for u in units if u.attrib.get("presence") == "equipped"]
    empty = [u for u in units if u.attrib.get("presence") != "equipped"]

    lines.append(f"### DIMM Slots ({len(populated)} of {len(units)} populated)")
    lines.append("")
    if populated:
        lines.append(
            "| Slot | Vendor | Capacity | Speed | Type | Operability |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for u in populated:
            a = u.attrib
            lines.append(
                f"| {a.get('id', '?')} | "
                f"{a.get('vendor', '?')} | "
                f"{a.get('capacity', '?')} MB | "
                f"{a.get('clock', '?')} MHz | "
                f"{a.get('type', '?')} | "
                f"{a.get('operability', '?')} |"
            )
    else:
        lines.append("_No DIMMs populated._")

    if empty:
        lines.append("")
        lines.append(f"_{len(empty)} empty slots: " +
                     ", ".join(u.attrib.get("id", "?") for u in empty) + "_")

    return "\n".join(lines)



# ----- SEL (System Event Log) tools -----

@mcp.tool(
    name="imc_get_event_log",
    annotations={
        "title": "Read System Event Log (SEL)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_get_event_log(params: EventLogInput) -> str:
    """Read entries from the System Event Log (SEL).

    Returns the most recent entries (up to max_entries, default 50). The SEL
    is where the IMC records hardware fault events, BIOS messages, sensor
    threshold crossings, and similar. Use this when investigating
    intermittent issues or after a hardware warning.

    Returns:
        str: SEL entries (newest first) in markdown or JSON.
    """
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    try:
        async with _imc_session() as (client, cookie):
            entries = await _resolve_class(client, cookie, "sysdebugMEpLogEntry")
    except Exception as exc:
        return _format_http_error(exc)

    # Sort newest first (by id if numeric, else by timestamp)
    def _sort_key(e: ET.Element) -> tuple:
        a = e.attrib
        rid = a.get("id", "0")
        try:
            return (1, -int(rid))
        except (ValueError, TypeError):
            return (0, a.get("timestamp", ""))

    entries.sort(key=_sort_key)

    if params.severity_filter:
        sev = params.severity_filter.lower()
        entries = [
            e for e in entries
            if sev in (e.attrib.get("severity") or "").lower()
        ]

    entries = entries[: params.max_entries]

    if params.response_format == ResponseFormat.JSON:
        return _to_json([_attrs(e) for e in entries])

    if not entries:
        msg = "_No SEL entries"
        if params.severity_filter:
            msg += f" matching severity '{params.severity_filter}'"
        msg += "._"
        return msg

    lines = [f"## System Event Log ({len(entries)} entries)", ""]
    lines.append("| ID | Time | Severity | Description |")
    lines.append("| --- | --- | --- | --- |")
    for e in entries:
        a = e.attrib
        desc = (a.get("description") or "").replace("|", "\\|")
        lines.append(
            f"| {a.get('id', '?')} | "
            f"{a.get('timestamp', '?')} | "
            f"{a.get('severity', '?')} | "
            f"{desc} |"
        )
    return "\n".join(lines)



@mcp.tool(
    name="imc_clear_event_log",
    annotations={
        "title": "Clear System Event Log (DESTRUCTIVE)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_clear_event_log(params: ClearLogInput) -> str:
    """Clear the entire System Event Log.

    Requires BOTH confirm=true AND i_understand_data_loss=true.

    Clearing the SEL is irreversible \u2014 the full event history (hardware
    fault events, BIOS messages, sensor threshold crossings) is permanently
    lost. Useful when the SEL is near full and you want a fresh starting
    point, but consider exporting the SEL first via imc_get_event_log.
    """
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return _missing_confirm_error("imc_clear_event_log")
    if not params.i_understand_data_loss:
        return _missing_data_loss_error("imc_clear_event_log")

    log_dn = f"{RACK_DN}/mgmt/log-SEL"
    try:
        async with _imc_session() as (client, cookie):
            await _conf_mo(
                client, cookie, log_dn, "sysdebugMEpLog",
                {"adminState": "clear"},
            )
    except Exception as exc:
        return _format_http_error(exc)

    return (
        f"OK: SEL cleared on {log_dn}. "
        "Use imc_get_event_log to confirm (should now be empty)."
    )


TOOLS = [
    # Inventory and status
    "imc_get_system_info", "imc_get_power_status", "imc_get_health",
    "imc_list_drives",
    # Power actions
    "imc_power_on", "imc_power_off_graceful",
    "imc_power_off_force", "imc_reboot", "imc_power_cycle",
    # Detailed health (new)
    "imc_get_psu_details", "imc_get_disk_smart", "imc_get_memory_health",
    # System Event Log (new)
    "imc_get_event_log", "imc_clear_event_log",
]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"-h", "--help"}:
        print(__doc__)
        print("Tools registered:")
        for t in TOOLS:
            print(f"  - {t}")
        sys.exit(0)
    mcp.run()
