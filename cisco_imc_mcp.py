"""Cisco IMC MCP Server (XML API / Nuova).

Manages a Cisco UCS C-Series or HyperFlex server via the IMC XML API,
which is supported on all IMC firmware versions (2.x, 3.x, 4.x).
Tested with: HX220C-M4S running IMC firmware 4.1(2m).

Configuration is loaded from environment variables (typically via .env):
    IMC_HOST       - IMC hostname or IP (e.g. 192.168.1.106)
    IMC_USERNAME   - IMC username (e.g. admin)
    IMC_PASSWORD   - IMC password
    IMC_VERIFY_SSL - "true" or "false" (default: false)
    IMC_TIMEOUT    - HTTP timeout seconds (default: 30)
    IMC_RACK_DN    - Rack-unit DN (default: sys/rack-unit-1)
"""
from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, AsyncIterator, Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

load_dotenv()

IMC_HOST = os.getenv("IMC_HOST", "").strip()
IMC_USERNAME = os.getenv("IMC_USERNAME", "").strip()
IMC_PASSWORD = os.getenv("IMC_PASSWORD", "")
IMC_VERIFY_SSL = os.getenv("IMC_VERIFY_SSL", "false").lower() == "true"
IMC_TIMEOUT = float(os.getenv("IMC_TIMEOUT", "30"))
RACK_DN = os.getenv("IMC_RACK_DN", "sys/rack-unit-1").strip()

mcp = FastMCP("cisco_imc_mcp")


def _require_config() -> Optional[str]:
    missing = []
    if not IMC_HOST:
        missing.append("IMC_HOST")
    if not IMC_USERNAME:
        missing.append("IMC_USERNAME")
    if not IMC_PASSWORD:
        missing.append("IMC_PASSWORD")
    if missing:
        return f"Error: Missing env vars: {', '.join(missing)}."
    return None


def _base_url() -> str:
    host = IMC_HOST
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    return host.rstrip("/")


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def _format_http_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Error: HTTP {exc.response.status_code} from IMC: {exc.response.text[:300]}"
    if isinstance(exc, httpx.ConnectError):
        return f"Error: Cannot connect to IMC at {IMC_HOST}."
    if isinstance(exc, httpx.TimeoutException):
        return f"Error: Request to IMC timed out after {IMC_TIMEOUT}s."
    if isinstance(exc, httpx.HTTPError):
        return f"Error: HTTP error: {exc}"
    return f"Error: {type(exc).__name__}: {exc}"


async def _post_xml(client: httpx.AsyncClient, body: str) -> ET.Element:
    resp = await client.post(
        "/nuova",
        content=body,
        headers={"Content-Type": "application/xml"},
    )
    resp.raise_for_status()
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        raise httpx.HTTPError(f"Failed to parse IMC XML: {e}") from e
    err = root.attrib.get("errorCode")
    if err and err != "0":
        descr = root.attrib.get("errorDescr", "(no description)")
        raise httpx.HTTPError(f"IMC API error {err}: {descr}")
    return root



@asynccontextmanager
async def _imc_session() -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Login, yield (client, cookie), and always logout on exit."""
    client = httpx.AsyncClient(
        base_url=_base_url(),
        verify=IMC_VERIFY_SSL,
        timeout=IMC_TIMEOUT,
    )
    cookie: Optional[str] = None
    try:
        login_body = (
            f'<aaaLogin inName="{_xml_escape(IMC_USERNAME)}" '
            f'inPassword="{_xml_escape(IMC_PASSWORD)}"></aaaLogin>'
        )
        root = await _post_xml(client, login_body)
        cookie = root.attrib.get("outCookie")
        if not cookie:
            raise httpx.HTTPError("IMC login returned no cookie (auth failed?).")
        yield client, cookie
    finally:
        if cookie:
            try:
                logout_body = f'<aaaLogout cookie="{cookie}" inCookie="{cookie}"/>'
                await _post_xml(client, logout_body)
            except Exception:
                pass
        await client.aclose()


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
            "Must be true to execute. The agent should only set this after "
            "the user has explicitly asked for the action."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        description="Optional short note about why the action is being taken.",
        max_length=200,
    )


def _missing_confirm_error(action: str) -> str:
    return (
        f"Refused: '{action}' requires confirm=true. "
        "Ask the user to confirm, then retry with confirm=true."
    )


def _attrs(el: ET.Element) -> dict[str, str]:
    return dict(el.attrib)


def _md_kv(title: str, pairs: list[tuple[str, Any]]) -> str:
    lines = [f"## {title}", ""]
    for k, v in pairs:
        if v in (None, "", "0"):
            continue
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines)


def _to_json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)



@mcp.tool(
    name="imc_get_system_info",
    annotations={
        "title": "Get Server System Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_get_system_info(params: FormatInput) -> str:
    """Get hardware summary of the rack-unit (model, serial, CPU, RAM, power state).

    Returns:
        str: Markdown- or JSON-formatted summary.
    """
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    try:
        async with _imc_session() as (client, cookie):
            unit = await _resolve_dn(client, cookie, RACK_DN)
            fw = await _resolve_dn(client, cookie, f"{RACK_DN}/mgmt/fw-system")
    except Exception as exc:
        return _format_http_error(exc)

    if unit is None:
        return f"Error: Rack-unit '{RACK_DN}' not found on IMC."

    u = _attrs(unit)
    fw_version = fw.attrib.get("version") if fw is not None else None

    if params.response_format == ResponseFormat.JSON:
        return _to_json({"rack_unit": u, "firmware_version": fw_version})

    mem_mb = u.get("totalMemory") or u.get("availableMemory")
    mem_gb = f"{int(mem_mb) / 1024:.0f} GB" if mem_mb and mem_mb.isdigit() else mem_mb
    pairs = [
        ("Vendor", u.get("vendor")),
        ("Model", u.get("model")),
        ("Name", u.get("name")),
        ("Serial Number", u.get("serial")),
        ("Power State", u.get("operPower")),
        ("Admin Power", u.get("adminPower")),
        ("BIOS POST", u.get("biosPostState")),
        ("CPUs", u.get("numOfCpus")),
        ("Cores", u.get("numOfCores")),
        ("Threads", u.get("numOfThreads")),
        ("Memory", mem_gb),
        ("IMC Firmware", fw_version),
    ]
    return _md_kv("Cisco IMC System Info", pairs)



@mcp.tool(
    name="imc_get_power_status",
    annotations={
        "title": "Get Server Power Status",
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
            unit = await _resolve_dn(client, cookie, RACK_DN)
    except Exception as exc:
        return _format_http_error(exc)
    if unit is None:
        return f"Error: Rack-unit '{RACK_DN}' not found."
    state = unit.attrib.get("operPower", "unknown")
    post = unit.attrib.get("biosPostState", "")
    extra = f" (BIOS POST: {post})" if post else ""
    return f"Power state: {state}{extra}"


@mcp.tool(
    name="imc_get_health",
    annotations={
        "title": "Get Server Health (PSU, Fans, Temps)",
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
            "power_supplies": [_attrs(x) for x in psus],
            "fans": [_attrs(x) for x in fans],
            "cpu_temps": [_attrs(x) for x in temps],
        })

    lines = ["## Server Health", ""]
    if psus:
        lines.append("### Power Supplies")
        for p in psus:
            a = p.attrib
            lines.append(f"- **{a.get('id', '?')}**: {a.get('model', '?')} - operability: {a.get('operability', '?')}")
        lines.append("")
    if fans:
        lines.append("### Fans")
        for f in fans:
            a = f.attrib
            lines.append(f"- **{a.get('id', '?')}**: operability: {a.get('operability', '?')}")
        lines.append("")
    if temps:
        lines.append("### CPU Temperatures")
        for t in temps:
            a = t.attrib
            lines.append(f"- {a.get('dn', '?')}: {a.get('temperature', '?')}C")
        lines.append("")
    if len(lines) == 2:
        lines.append("_No health data; server may be powered off._")
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
        return "_No drives reported by IMC._"

    lines = ["## Physical Drives", ""]
    for d in drives:
        a = d.attrib
        size_mb = a.get("coercedSize") or a.get("rawSize", "?")
        try:
            size_gb = f"{int(size_mb) / 1024:.0f} GB"
        except (ValueError, TypeError):
            size_gb = f"{size_mb} MB"
        lines.append(
            f"- **slot {a.get('id', '?')}**: "
            f"{a.get('vendor', '?')} {a.get('productId') or a.get('productName', '?')} - "
            f"{size_gb} - "
            f"type: {a.get('mediaType', '?')} - "
            f"state: {a.get('driveState', a.get('pdStatus', '?'))}"
        )
    return "\n".join(lines)



async def _set_admin_power(value: str) -> str:
    """Set adminPower on the rack-unit. Returns a result message."""
    try:
        async with _imc_session() as (client, cookie):
            root = await _conf_mo(
                client, cookie, RACK_DN, "computeRackUnit",
                {"adminPower": value},
            )
        out = root.find("outConfig")
        if out is not None and len(out) > 0:
            new_state = out[0].attrib.get("operPower", "?")
            admin = out[0].attrib.get("adminPower", "?")
            return (
                f"OK: Sent adminPower='{value}' to {RACK_DN}. "
                f"Now operPower='{new_state}', adminPower='{admin}'. "
                "Physical state may take 30-90s to settle; use imc_get_power_status."
            )
        return f"OK: Sent adminPower='{value}' to {RACK_DN}."
    except Exception as exc:
        return _format_http_error(exc)


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
    """Power on the server. Requires confirm=true.

    Returns:
        str: Result of the action.
    """
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return _missing_confirm_error("imc_power_on")
    return await _set_admin_power("up")



@mcp.tool(
    name="imc_power_off_graceful",
    annotations={
        "title": "Graceful Shutdown",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def imc_power_off_graceful(params: ConfirmInput) -> str:
    """Request a graceful OS shutdown via ACPI. Requires confirm=true."""
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return _missing_confirm_error("imc_power_off_graceful")
    return await _set_admin_power("soft-shut-down")


@mcp.tool(
    name="imc_power_off_force",
    annotations={
        "title": "Force Power Off",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def imc_power_off_force(params: ConfirmInput) -> str:
    """Immediately cut power. May cause data loss. Requires confirm=true."""
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return _missing_confirm_error("imc_power_off_force")
    return await _set_admin_power("down")



@mcp.tool(
    name="imc_reboot",
    annotations={
        "title": "Hard Reset Server",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def imc_reboot(params: ConfirmInput) -> str:
    """Force restart (hard reset) the server. Requires confirm=true."""
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return _missing_confirm_error("imc_reboot")
    return await _set_admin_power("hard-reset-immediate")


@mcp.tool(
    name="imc_power_cycle",
    annotations={
        "title": "Power Cycle (Off then On)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def imc_power_cycle(params: ConfirmInput) -> str:
    """Power cycle the server (off then on). Requires confirm=true."""
    cfg_err = _require_config()
    if cfg_err:
        return cfg_err
    if not params.confirm:
        return _missing_confirm_error("imc_power_cycle")
    return await _set_admin_power("cycle-immediate")


TOOLS = [
    "imc_get_system_info", "imc_get_power_status", "imc_get_health",
    "imc_list_drives", "imc_power_on", "imc_power_off_graceful",
    "imc_power_off_force", "imc_reboot", "imc_power_cycle",
]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"-h", "--help"}:
        print(__doc__)
        print("Tools registered:")
        for t in TOOLS:
            print(f"  - {t}")
        sys.exit(0)
    mcp.run()
