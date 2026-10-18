# SPDX-License-Identifier: LGPL-2.1-or-later
"""Manage routes in an existing, operator-provisioned system Traefik."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

NETWORK = "cockpit-traefik"
PROXY = "cockpit-traefik"
CONFIG_HEADER = "# Managed by cockpit-podman\n"


def command(*args: str) -> str:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Command failed: " + args[0])
    return result.stdout


def atomic_text(path: Path, value: str) -> None:
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".cockpit-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def unsafe_directory(path: Path) -> Optional[str]:
    """Explain why a directory written by the privileged helper is unsafe, or return None."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None  # reported separately as a missing prerequisite
    if not stat.S_ISDIR(info.st_mode):
        return str(path) + " must be a real directory, not a symlink or file"
    if info.st_uid != os.geteuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return str(path) + " must be owned by root and not writable by group or others"
    return None


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, indent=2))


def port(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not str(value).isascii()
        or not str(value).isdigit()
        or not 1 <= int(value) <= 65535
    ):
        raise ValueError("Port must be an integer between 1 and 65535")
    return int(value)


def route_value(value: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("host", ""), str):
        raise ValueError("Enter a hostname without scheme, port or wildcard")
    raw = unicodedata.normalize("NFC", value.get("host", "").rstrip(".")).lower()
    host = raw.encode("idna").decode("ascii").lower()
    # IDNA2003 silently maps e.g. "ß" to "ss"; browsers send a different name.
    if not raw.isascii() and host.encode("ascii").decode("idna") != raw:
        raise ValueError("Enter internationalized hostnames in punycode (xn--) form")
    if len(host) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")
    ):
        raise ValueError("Enter a hostname without scheme, port or wildcard")
    paths = {}
    for key in ("path", "internal_path"):
        path = value.get(key, "/")
        if not isinstance(path, str):
            raise ValueError("Paths must be strings starting with /")
        path = path or "/"
        if not path.startswith("/") or re.search(r"[\s`?#\\\x00-\x1f\x7f]", path):
            raise ValueError(
                "Paths must start with / and cannot contain whitespace, query strings or rule delimiters"
            )
        paths[key] = path
    tls = value.get("tls", "http")
    if tls not in ("http", "letsencrypt", "certificate"):
        raise ValueError("Unknown TLS mode")
    for key in ("enabled", "strip_path"):
        if key in value and not isinstance(value[key], bool):
            raise ValueError(key + " must be boolean")
    return dict(
        host=host,
        port=port(value.get("port", 80)),
        tls=tls,
        enabled=value.get("enabled", True),
        strip_path=value.get("strip_path", False),
        **paths,
    )


def route_id(route: Dict[str, Any]) -> str:
    return hashlib.sha256((route["host"] + "\0" + route["path"]).encode()).hexdigest()[:24]


def dynamic_config(routes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    http: Dict[str, Dict[str, Any]] = {"routers": {}, "services": {}, "middlewares": {}}
    for key, route in routes.items():
        if not route["enabled"]:
            continue
        middlewares = []
        if route["strip_path"] and route["path"] != "/":
            name = key + "-strip"
            http["middlewares"][name] = {"stripPrefix": {"prefixes": [route["path"]]}}
            middlewares.append(name)
        if route["internal_path"] != "/" and route["internal_path"] != route["path"]:
            name = key + "-add"
            http["middlewares"][name] = {"addPrefix": {"prefix": route["internal_path"]}}
            middlewares.append(name)
        rule = "Host(`" + route["host"] + "`)"
        if route["path"] != "/":
            rule += " && PathPrefix(`" + route["path"] + "`)"
        router = {"rule": rule, "service": key, "entryPoints": ["web"], "middlewares": middlewares}
        if route["tls"] != "http":
            redirect = key + "-redirect"
            http["middlewares"][redirect] = {"redirectScheme": {"scheme": "https", "permanent": True}}
            http["routers"][key + "-http"] = dict(router, middlewares=[redirect])
            router = dict(router, entryPoints=["websecure"], tls={})
            if route["tls"] == "letsencrypt":
                router["tls"] = {"certResolver": "letsencrypt"}
        http["routers"][key] = router
        http["services"][key] = {
            "loadBalancer": {
                "servers": [{"url": "http://" + route["container"] + ":" + str(route["port"])}],
                "passHostHeader": True,
            }
        }
    certificates = [
        {
            "certFile": "/etc/traefik/certs/cockpit-podman/" + key + ".crt",
            "keyFile": "/etc/traefik/certs/cockpit-podman/" + key + ".key",
        }
        for key, route in routes.items()
        if route["tls"] == "certificate"
    ]
    # Traefik's YAML decoder rejects empty standalone maps such as middlewares: {}.
    http = {key: value for key, value in http.items() if value}
    config: Dict[str, Any] = {"http": http} if http else {}
    if certificates:
        config["tls"] = {"certificates": certificates}
    return config


def route_backends(
    routes: Dict[str, Dict[str, Any]], containers: List[Dict[str, Any]], network: str
) -> Dict[str, Dict[str, Any]]:
    """Annotate routes with their backend container state (from `podman ps --all --format json`)."""
    by_name = {name: item for item in containers for name in item.get("Names") or []}
    result = {}
    for key, route in routes.items():
        item = by_name.get(route["container"])
        if item is None:
            backend = "missing"
        elif not route["enabled"]:
            backend = "ok"
        elif item.get("State") != "running":
            backend = "stopped"
        elif network not in (item.get("Networks") or []):
            backend = "detached"
        else:
            backend = "ok"
        labels = (item or {}).get("Labels") or {}
        result[key] = dict(route, backend=backend, systemd_unit=labels.get("PODMAN_SYSTEMD_UNIT", ""))
    return result


class Manager:
    def __init__(self, root: Optional[Path] = None, connection: Optional[Dict[str, Any]] = None) -> None:
        if os.geteuid() != 0:
            raise PermissionError("The shared Traefik requires system containers and administrative access")
        if connection is None:
            path = Path("/etc/cockpit/traefik.json")
            connection = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(connection, dict):
            raise ValueError("Traefik connection settings must be a JSON object")
        self.proxy = connection.get("container", PROXY)
        self.network = connection.get("network", NETWORK)
        for value in (self.proxy, self.network):
            if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value):
                raise ValueError("Enter an existing Podman container and network name")
        directory = connection.get("directory", "/var/lib/cockpit-traefik")
        if not isinstance(directory, str) or not Path(directory).is_absolute():
            raise ValueError("The Traefik routing directory must be an absolute path")
        self.root = Path(root) if root else Path(directory)

    def state(self) -> Dict[str, Any]:
        path = self.root / "cockpit-podman-state.json"
        state = json.loads(path.read_text()) if path.exists() else {"routes": {}}
        return {"routes": state["routes"]}

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        status = self.status()
        if not status["ready"]:
            raise ValueError("; ".join(status["prerequisites"]))
        (self.root / "certs/cockpit-podman").mkdir(mode=0o700, exist_ok=True)
        problem = unsafe_directory(self.root / "certs/cockpit-podman")
        if problem:
            raise ValueError(problem)
        fd = os.open(self.root / "cockpit-podman.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def publish(self, state: Dict[str, Any]) -> None:
        old = self.state()
        atomic_json(self.root / "cockpit-podman-state.json", state)
        try:
            # JSON is a YAML subset; directory providers only discover .yml/.yaml/.toml.
            atomic_text(
                self.root / "dynamic/cockpit-podman.yml",
                CONFIG_HEADER + json.dumps(dynamic_config(state["routes"]), indent=2),
            )
        except Exception:
            atomic_json(self.root / "cockpit-podman-state.json", old)
            raise

    def status(self) -> Dict[str, Any]:
        state = self.state()
        issues = []
        state["running"] = False
        exists = subprocess.run(["podman", "container", "exists", self.proxy], capture_output=True)
        if exists.returncode == 1:
            issues.append("Install and start the system Traefik container named " + self.proxy + " first")
        elif exists.returncode:
            raise RuntimeError(exists.stderr.decode().strip() or "Cannot inspect existing Traefik")
        else:
            container = json.loads(command("podman", "inspect", self.proxy))[0]
            state["running"] = bool(container["State"]["Running"])
            if not state["running"]:
                issues.append("Start the existing Traefik outside Cockpit; it will not be started automatically")
            else:
                image_version = (container.get("Config", {}).get("Labels") or {}).get(
                    "org.opencontainers.image.version", ""
                )
                # Derived images often overwrite the version label, so only a matching label skips the exec check.
                if not re.match(r"v?3\.", image_version):
                    try:
                        version = command("podman", "exec", self.proxy, "traefik", "version")
                        if not re.search(r"^Version:\s+3\.", version, re.MULTILINE):
                            issues.append("The existing proxy must provide Traefik 3")
                    except RuntimeError:
                        issues.append("The existing proxy must provide the traefik executable")
            if self.network not in container.get("NetworkSettings", {}).get("Networks", {}):
                issues.append("Connect the existing Traefik to the " + self.network + " network first")
            user = container.get("Config", {}).get("User", "").split(":")[0]
            if user not in ("", "0", "root") or container.get("HostConfig", {}).get("UsernsMode", "") not in (
                "",
                "host",
            ):
                issues.append("The system Traefik must use its default root identity to read private routing files")
            for directory in ("dynamic", "certs"):
                if not any(
                    mount.get("Destination") == "/etc/traefik/" + directory
                    and Path(mount.get("Source", "")).resolve() == (self.root / directory).resolve()
                    for mount in container.get("Mounts", [])
                ):
                    issues.append("Bind-mount " + str(self.root / directory) + " at /etc/traefik/" + directory)
        network = subprocess.run(["podman", "network", "exists", self.network], capture_output=True)
        if network.returncode == 1:
            issues.append("Create the DNS-enabled " + self.network + " bridge outside Cockpit first")
        elif network.returncode:
            raise RuntimeError(network.stderr.decode().strip() or "Cannot inspect existing proxy network")
        else:
            info = json.loads(command("podman", "network", "inspect", self.network))[0]
            if not info.get("dns_enabled") or info.get("driver") != "bridge":
                issues.append("The existing proxy network must be a DNS-enabled bridge")
        for directory in ("dynamic", "certs"):
            if not (self.root / directory).is_dir():
                issues.append("Prepare the directory " + str(self.root / directory) + " before using domain routing")
        for path in (self.root, self.root / "dynamic", self.root / "certs", self.root / "certs/cockpit-podman"):
            problem = unsafe_directory(path)
            if problem:
                issues.append(problem)
        configuration = self.root / "dynamic/cockpit-podman.yml"
        if configuration.exists():
            if not configuration.read_text().startswith(CONFIG_HEADER):
                issues.append("Refusing to replace an existing configuration not managed by cockpit-podman")
            elif not (self.root / "cockpit-podman-state.json").exists():
                issues.append("Restore the missing cockpit-podman route state before changing existing routes")
        containers = json.loads(command("podman", "ps", "--all", "--format", "json") or "[]")
        state["routes"] = route_backends(state["routes"], containers, self.network)
        state["prerequisites"] = issues
        state["ready"] = not issues
        state["proxy"] = self.proxy
        state["network"] = self.network
        return state

    def certificate(self, request: Dict[str, Any], key: str, old_id: Optional[str]) -> None:
        certificate = request.get("certificate", "")
        private_key = request.get("private_key", "")
        if not certificate and not private_key:
            if not old_id or not (self.root / "certs/cockpit-podman" / (old_id + ".crt")).exists():
                raise ValueError("Supply a PEM certificate chain and private key")
            certificate = (self.root / "certs/cockpit-podman" / (old_id + ".crt")).read_text()
            private_key = (self.root / "certs/cockpit-podman" / (old_id + ".key")).read_text()
        with tempfile.TemporaryDirectory() as directory:
            cert_path, key_path = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            atomic_text(cert_path, certificate)
            atomic_text(key_path, private_key)
            # A callback prevents an encrypted key from prompting on the Cockpit channel.
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert_path, key_path, password=lambda: "")
        atomic_text(self.root / "certs/cockpit-podman" / (key + ".crt"), certificate)
        atomic_text(self.root / "certs/cockpit-podman" / (key + ".key"), private_key)

    def save(self, request: Dict[str, Any]) -> Dict[str, Any]:
        route = route_value(request["route"])
        with self.locked():
            state = self.state()
            old_id = request.get("id")
            if old_id and old_id not in state["routes"]:
                raise ValueError("Route no longer exists; refresh the list")
            container = json.loads(command("podman", "container", "inspect", "--", request["container"]))[0]
            name = container["Name"].lstrip("/")
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name) or name == self.proxy:
                raise ValueError("Select an application container")
            route["container"] = name
            if old_id and state["routes"][old_id]["container"] != name:
                raise ValueError("Cannot edit another container's route")
            key = route_id(route)
            if key in state["routes"] and key != old_id:
                raise ValueError(
                    "This hostname and path already have a route for container " + state["routes"][key]["container"]
                )
            previous: Dict[Path, Optional[str]] = {}
            try:
                if route["tls"] == "certificate":
                    for suffix in (".crt", ".key"):
                        path = self.root / "certs/cockpit-podman" / (key + suffix)
                        previous[path] = path.read_text() if path.exists() else None
                    self.certificate(request, key, old_id)
                if route["enabled"]:
                    mode = container.get("HostConfig", {}).get("NetworkMode", "")
                    if container.get("Pod") or mode in ("host", "none") or mode.startswith("container:"):
                        raise ValueError("Routes require a standalone container on a DNS-enabled bridge network")
                    if mode.startswith(("pasta", "slirp4netns")):
                        raise ValueError(
                            "This container uses user-mode networking; use a bridge network before adding a route"
                        )
                    networks = container.get("NetworkSettings", {}).get("Networks", {})
                    if self.network not in networks:
                        command("podman", "network", "connect", self.network, container["Id"])
                if old_id:
                    del state["routes"][old_id]
                state["routes"][key] = route
                self.publish(state)
            except Exception:
                # Undo certificate files written for a route that was not published.
                for path, text in previous.items():
                    if text is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_text(path, text)
                raise
            self.prune_certificates(state)
        return self.status()

    def delete(self, request: Dict[str, Any]) -> Dict[str, Any]:
        with self.locked():
            state = self.state()
            route = state["routes"].get(request["id"])
            owner = request["container"].lstrip("/")
            if not route or (
                route["container"] != owner
                and subprocess.run(
                    ["podman", "container", "exists", route["container"]], capture_output=True
                ).returncode != 1
            ):
                raise ValueError("Route not found for this container")
            del state["routes"][request["id"]]
            self.publish(state)
            self.prune_certificates(state)
        return self.status()

    def prune_certificates(self, state: Dict[str, Any]) -> None:
        retained = {key for key, route in state["routes"].items() if route["tls"] == "certificate"}
        for path in (self.root / "certs/cockpit-podman").iterdir():
            if re.fullmatch(r"[a-f0-9]{24}\.(crt|key)", path.name) and path.stem not in retained:
                path.unlink()


def main() -> None:
    request = json.load(sys.stdin)
    if not isinstance(request, dict):
        raise ValueError("Expected a JSON object")
    manager = Manager()
    action = request.get("action")
    if action not in ("status", "save", "delete"):
        raise ValueError("Unknown action")
    print(json.dumps(getattr(manager, action)() if action == "status" else getattr(manager, action)(request)))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
