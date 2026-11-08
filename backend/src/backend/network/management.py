"""Detection and persistent configuration adapters for Linux network managers."""

from __future__ import annotations
from common.identity import APP_NAME, STORAGE_ID, SESSION_COOKIE, SESSION_SALT, SOURCE_KEY, MANIFEST_NAME, DEFINITIONS_DIRECTORY, TEMP_PREFIX, MANAGED_HEADER

import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

INTERFACE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")


class NetworkManagementError(Exception):
    pass


def _networkd_links() -> list[dict]:
    links: list[dict] = []
    directory = Path("/run/systemd/netif/links")
    if not directory.is_dir():
        return links
    for path in directory.iterdir():
        values: dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key.lower()] = value
        values["interface_index"] = path.name
        links.append(values)
    return links


def _ifupdown_state() -> list[str]:
    try:
        content = Path("/run/network/ifstate").read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.split("=", 1)[0] for line in content.splitlines() if "=" in line]


def detect(networkd_directory: Path, interfaces_file: Path) -> dict:
    networkd_files = sorted(str(path) for path in networkd_directory.glob("*.network"))
    networkd_runtime = Path("/run/systemd/netif").is_dir()
    ifupdown_present = interfaces_file.is_file()
    managers = []
    if networkd_runtime or networkd_files:
        managers.append(
            {
                "name": "systemd-networkd",
                "active": networkd_runtime,
                "tool": shutil.which("networkctl"),
                "configuration_directory": str(networkd_directory),
                "configuration_files": networkd_files,
                "managed_links": _networkd_links(),
                "persistent": True,
            }
        )
    if ifupdown_present:
        managers.append(
            {
                "name": "ifupdown",
                "active": bool(_ifupdown_state()),
                "tools": {
                    "ifup": shutil.which("ifup"),
                    "ifdown": shutil.which("ifdown"),
                    "ifreload": shutil.which("ifreload"),
                },
                "configuration_file": str(interfaces_file),
                "managed_interfaces": _ifupdown_state(),
                "persistent": True,
            }
        )
    return {
        "detected": managers,
        "preferred": next((item["name"] for item in managers if item["active"]), None)
        or (managers[0]["name"] if managers else None),
        "temporary_backend": "rtnetlink",
    }


def _validated(config: dict[str, Any]) -> dict[str, Any]:
    interface = config.get("interface", "")
    if not isinstance(interface, str) or not INTERFACE_PATTERN.fullmatch(interface):
        raise NetworkManagementError("Invalid interface name")
    addresses = []
    for value in config.get("addresses", []):
        try:
            addresses.append(str(ipaddress.ip_interface(value)))
        except ValueError as exc:
            raise NetworkManagementError(f"Invalid address: {value}") from exc
    gateways = []
    for value in config.get("gateways", []):
        try:
            gateways.append(str(ipaddress.ip_address(value)))
        except ValueError as exc:
            raise NetworkManagementError(f"Invalid gateway: {value}") from exc
    dns = []
    for value in config.get("dns", []):
        try:
            dns.append(str(ipaddress.ip_address(value)))
        except ValueError as exc:
            raise NetworkManagementError(f"Invalid DNS server: {value}") from exc
    return {
        "interface": interface,
        "addresses": addresses,
        "gateways": gateways,
        "dns": dns,
        "dhcp4": bool(config.get("dhcp4", False)),
        "dhcp6": bool(config.get("dhcp6", False)),
        "auto": bool(config.get("auto", True)),
    }


def _networkd_content(config: dict[str, Any]) -> str:
    dhcp = "yes" if config["dhcp4"] and config["dhcp6"] else (
        "ipv4" if config["dhcp4"] else "ipv6" if config["dhcp6"] else "no"
    )
    lines = [MANAGED_HEADER, "[Match]", f"Name={config['interface']}", "", "[Network]", f"DHCP={dhcp}"]
    lines.extend(f"Address={value}" for value in config["addresses"])
    lines.extend(f"Gateway={value}" for value in config["gateways"])
    lines.extend(f"DNS={value}" for value in config["dns"])
    return "\n".join(lines) + "\n"


def _ifupdown_content(config: dict[str, Any]) -> str:
    lines = [MANAGED_HEADER]
    interface = config["interface"]
    if config["auto"]:
        lines.append(f"auto {interface}")
    for family, version, dhcp_key in (("inet", 4, "dhcp4"), ("inet6", 6, "dhcp6")):
        addresses = [value for value in config["addresses"] if ipaddress.ip_interface(value).version == version]
        gateways = [value for value in config["gateways"] if ipaddress.ip_address(value).version == version]
        if not addresses and not config[dhcp_key]:
            continue
        method = "dhcp" if config[dhcp_key] else "static"
        lines.append(f"iface {interface} {family} {method}")
        for value in addresses:
            lines.append(f"    address {value}")
        for value in gateways:
            lines.append(f"    gateway {value}")
        if config["dns"]:
            lines.append(f"    dns-nameservers {' '.join(config['dns'])}")
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _ifupdown_overlay_is_safe(interfaces_file: Path, interface: str, target: Path) -> None:
    if not interfaces_file.exists():
        return
    try:
        main_content = interfaces_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise NetworkManagementError(f"Unable to read {interfaces_file}: {exc}") from exc
    include_pattern = re.compile(
        r"^\s*source(?:-directory)?\s+.*interfaces\.d(?:/\*|\s|$)", re.MULTILINE
    )
    if not include_pattern.search(main_content):
        raise NetworkManagementError(
            f"{interfaces_file} does not include interfaces.d; refusing to edit the main file"
        )
    stanza = re.compile(rf"^\s*iface\s+{re.escape(interface)}\s+", re.MULTILINE)
    if stanza.search(main_content):
        raise NetworkManagementError(
            f"Interface {interface} already has a stanza in {interfaces_file}"
        )
    for path in target.parent.glob("*"):
        if path == target or not path.is_file():
            continue
        try:
            if stanza.search(path.read_text(encoding="utf-8")):
                raise NetworkManagementError(
                    f"Interface {interface} already has a stanza in {path}"
                )
        except UnicodeDecodeError:
            continue


def _run(command: list[str], timeout: float) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or '').strip()[:4000]
        raise NetworkManagementError(
            f'{command[0]} exited with status {exc.returncode}: {detail or "no diagnostic output"}'
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NetworkManagementError(f"Command failed: {command[0]}: {exc}") from exc


def configure(
    manager: str,
    raw_config: dict[str, Any],
    networkd_directory: Path,
    interfaces_file: Path,
    *,
    commit: bool = False,
    activate: bool = False,
    timeout: float = 15.0,
) -> dict:
    config = _validated(raw_config)
    interface = config["interface"]
    commands: list[list[str]] = []
    if manager == "systemd-networkd":
        # networkd uses the first lexically matching file, so application's explicit
        # configuration must sort before generic distribution files.
        target = networkd_directory / f"00-{STORAGE_ID}-{interface}.network"
        content = _networkd_content(config)
        tool = shutil.which("networkctl") or "networkctl"
        commands = [[tool, "reload"], [tool, "reconfigure", interface]]
    elif manager == "ifupdown":
        target = interfaces_file.parent / "interfaces.d" / f"90-{STORAGE_ID}-{interface}"
        content = _ifupdown_content(config)
        if ifreload := shutil.which("ifreload"):
            commands = [[ifreload, "-i", interface]]
        else:
            commands = [
                [shutil.which("ifdown") or "ifdown", "--force", interface],
                [shutil.which("ifup") or "ifup", interface],
            ]
    else:
        raise NetworkManagementError("Unsupported network manager")
    result = {
        "manager": manager,
        "interface": interface,
        "target": str(target),
        "content": content,
        "commands": commands,
        "committed": False,
        "activated": False,
    }
    if commit:
        if manager == "ifupdown":
            _ifupdown_overlay_is_safe(interfaces_file, interface, target)
        _atomic_write(target, content)
        result["committed"] = True
    if activate:
        if not commit:
            raise NetworkManagementError("activate requires commit=true")
        for command in commands:
            _run(command, timeout)
        result["activated"] = True
    return result
