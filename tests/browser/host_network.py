import json
import re
import socket
import sys

INTERNAL_PORTS = (5900, 6080, 8384)
CONTROL_PORTS = (8000, 22000)
LOOPBACKS = ("127.0.0.1", "::1")
ALLOWED = {8000, 22000, 21027}
NEEDED = {8000, 22000}
SOCKETS = ("/run/darkroom/vnc.sock", "/run/darkroom/viewer.sock", "/run/darkroom/syncthing.sock")
DISPLAY = "\0/tmp/.X11-unix/X99"
OWNER = re.compile(r"pid=(\d+),")


def answers(host, port):
    for family, kind, proto, _, address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        try:
            stream = socket.socket(family, kind, proto)
        except OSError:
            continue
        with stream:
            stream.settimeout(3)
            if stream.connect_ex(address) == 0:
                return True
    return False


def unix_answers(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(3)
        try:
            stream.connect(path)
        except OSError as error:
            return type(error).__name__
    return "connected"


def reached(hosts):
    return {host: [port for port in CONTROL_PORTS if answers(host, port)] for host in hosts}


def refused(hosts):
    found = {f"{host}:{port}": answers(host, port) for host in hosts for port in INTERNAL_PORTS}
    found["abstract display socket"] = unix_answers(DISPLAY)
    control = reached(hosts)
    print(json.dumps({"internal": found, "published": control}, sort_keys=True))
    return 0 if all(control.values()) and not any(value is True for value in found.values()) and found["abstract display socket"] != "connected" else 1


def inside():
    found = {f"{host}:{port}": answers(host, port) for host in LOOPBACKS for port in INTERNAL_PORTS}
    found |= {path: unix_answers(path) for path in SOCKETS}
    control = answers("127.0.0.1", 8000)
    print(json.dumps({"internal": found, "app": control}, sort_keys=True))
    return 0 if control and not any(value is True for value in found.values()) and all(found[path] == "PermissionError" for path in SOCKETS) else 1


def owned(lines, pids):
    return [line for line in lines if {int(pid) for pid in OWNER.findall(line)} & pids]


def listeners(pids):
    lines = owned(sys.stdin.read().splitlines(), pids)
    print("\n".join(lines))
    ports = {int(line.split()[4].rsplit(":", 1)[1]) for line in lines}
    print(json.dumps({"ports": sorted(ports)}))
    return 0 if ports <= ALLOWED and ports >= NEEDED else 1


def unix_listeners(pids):
    lines = owned(sys.stdin.read().splitlines(), pids)
    print("\n".join(lines))
    abstract = [line for line in lines if line.split()[4].startswith("@")]
    paths = {line.split()[4] for line in lines}
    return 0 if not abstract and set(SOCKETS) <= paths else 1


def main(command, *args):
    commands = {"refused": lambda: refused(args), "inside": inside, "listeners": lambda: listeners({int(pid) for pid in args}),
                "unix-listeners": lambda: unix_listeners({int(pid) for pid in args})}
    return commands[command]()


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
