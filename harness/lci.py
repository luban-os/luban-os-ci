#!/usr/bin/env python3
"""luban-os-ci's harness: Luban as a person and a machine meet it.

    ./run.sh <layer> [scenario-id-or-glob ...] [options]      (README.md)

It drives the programs under test -- `xlings`, `luban`, `luban-os
--headless --script`, `xdesk msg` -- in a home of its own, from data
(scenarios/<layer>/*.toml, fixtures.toml), and links nothing of xlings.
Every run says what it ran, what it did not and why, in report.json and
report.md under its work directory. A probe that fails fails the scenarios
that need it: a run that tested nothing never shows as passed.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")
LAYERS = ("L2", "L3", "L4", "L5", "L6")


def log(msg: str) -> None:
    print(f"[lci] {msg}", flush=True)


def plain(text: str) -> str:
    return ANSI.sub("", text)


# ── the programs under test ─────────────────────────────────────────


@dataclass
class Programs:
    xlings: Path
    luban: Path
    luban_os: Path | None = None
    xdesk: Path | None = None
    version: str = ""


def arch() -> str:
    m = os.uname().machine
    return {"arm64": "aarch64", "amd64": "x86_64"}.get(m, m)


def download(url: str, to: Path) -> Path:
    to.parent.mkdir(parents=True, exist_ok=True)
    log(f"download {url}")
    with urllib.request.urlopen(url, timeout=300) as r, to.open("wb") as f:
        shutil.copyfileobj(r, f)
    return to


def latest_version() -> str:
    req = urllib.request.Request("https://api.github.com/repos/openxlings/xlings/releases/latest",
                                 headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["tag_name"].lstrip("v")


def release_programs(version: str, cache: Path) -> Programs:
    """The released archives of `version` (latest: the newest), unpacked once."""
    v = latest_version() if version == "latest" else version.lstrip("v")
    base = f"https://github.com/openxlings/xlings/releases/download/v{v}"
    out = cache / v
    found: dict[str, Path] = {}
    for name, required in (("xlings", True), ("luban", True), ("luban-os", False), ("xdesk", False)):
        archive = cache / f"{name}-{v}-linux-{arch()}.tar.gz"
        try:
            if not archive.exists():
                download(f"{base}/{archive.name}", archive)
        except Exception as e:  # noqa: BLE001 -- a missing optional archive is said, not fatal
            if required:
                raise SystemExit(f"lci: {archive.name}: {e}")
            log(f"{archive.name}: not published here ({e})")
            continue
        dest = out / name
        if not dest.exists():
            with tarfile.open(archive) as t:
                t.extractall(dest, filter="tar")
        hits = [p for p in dest.rglob(name) if p.is_file() and p.parent.name == "bin"]
        if hits:
            found[name] = hits[0]
    if "xlings" not in found or "luban" not in found:
        raise SystemExit(f"lci: release {v} has no xlings or luban binary")
    return Programs(found["xlings"], found["luban"], found.get("luban-os"), found.get("xdesk"), v)


def directory_programs(d: Path) -> Programs:
    """A directory of built binaries (an xlings build's target, a CI artifact)."""
    def pick(name: str) -> Path | None:
        for p in [d / name, d / "bin" / name, *d.rglob(name)]:
            if p.is_file() and os.access(p, os.X_OK):
                return p
        return None
    x, lb = pick("xlings"), pick("luban")
    if not x or not lb:
        raise SystemExit(f"lci: {d} has no xlings or no luban")
    return Programs(x, lb, pick("luban-os"), pick("xdesk"))


# ── the home a run happens in ───────────────────────────────────────


PASSED_ENV = ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "NO_PROXY", "XLINGS_RELEASE_MIRROR", "XLINGS_TEST_MIRROR", "TERM", "LANG")


class Home:
    """An isolated xlings home with the programs under test: nothing of the
    person running it is read or written (HOME, XLINGS_HOME are its own)."""

    def __init__(self, work: Path, programs: Programs, index: str | None, setup: list[str] = ()):
        self.work = work
        self.dir = work / "home"
        self.user = work / "user"
        self.programs = programs
        for d in (self.dir / "bin", self.user):
            d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(programs.xlings, self.dir / "bin" / "xlings")
        for name in ("luban", "luban-init"):
            shutil.copy2(programs.luban, self.dir / "bin" / name)
        for name, p in (("luban-os", programs.luban_os), ("xdesk", programs.xdesk)):
            if p:
                shutil.copy2(p, self.dir / "bin" / name)
        r = self.sh("xlings self init", timeout=300)
        if r.code != 0:
            raise SystemExit(f"lci: xlings self init: {r.out[-800:]}")
        if index:
            self.use_index(index)
        for c in setup:
            log(f"setup: {c}")
            r = self.sh(c, timeout=900)
            if r.code != 0:
                raise SystemExit(f"lci: setup {c}: {r.out[-800:]}")

    def env(self) -> dict[str, str]:
        e = {k: os.environ[k] for k in PASSED_ENV if k in os.environ}
        e.update(HOME=str(self.user), XLINGS_HOME=str(self.dir), USER=os.environ.get("USER", "lci"),
                 PATH=f"{self.dir / 'subos' / 'current' / 'bin'}:{self.dir / 'bin'}:/usr/bin:/bin",
                 LUBAN_XLINGS=str(self.dir / "bin" / "xlings"), XLINGS_AGENT_MODE="1")
        e.setdefault("TERM", "dumb")
        return e

    def sh(self, command: str, timeout: int = 600, stdin: str | None = None) -> "Ran":
        return run(["sh", "-c", command], self.env(), timeout, stdin)

    def use_index(self, index: str) -> None:
        """An index under test: a directory (a checkout) or a branch of
        openxlings/xim-pkgindex -- as xlings' own CI gives one
        (tools/ci/xlings_tools.sh): the home's only index repository, the
        index synced before it gone. The checkout is inside the home, by its
        absolute path: a machine sees the home, and nothing beside it."""
        src = self.dir / "index-under-test"
        if Path(index).is_dir():
            files = subprocess.run(["git", "-C", index, "ls-files", "-z"], capture_output=True, check=True).stdout
            src.mkdir()
            for rel in filter(None, files.split(b"\0")):
                s = Path(index) / rel.decode(errors="surrogateescape")
                if s.is_file():
                    t = src / rel.decode(errors="surrogateescape")
                    t.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(s, t)
        else:
            subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", index,
                            "https://github.com/openxlings/xim-pkgindex.git", str(src)], check=True)
        cfg_file = self.dir / ".xlings.json"
        cfg = json.loads(cfg_file.read_text()) if cfg_file.exists() else {}
        cfg["index_repos"] = [{"name": "xim", "url": str(src)}]
        cfg_file.write_text(json.dumps(cfg, indent=2))
        synced = self.dir / "data" / "xim-pkgindex"
        if synced.is_symlink() or synced.is_file():
            synced.unlink()
        elif synced.is_dir():
            shutil.rmtree(synced)
        r = self.sh("xlings update", timeout=600)
        if r.code != 0:
            raise SystemExit(f"lci: xlings update (the index under test): {r.out[-800:]}")
        if synced.resolve() != src.resolve():
            raise SystemExit(f"lci: the index in use is {synced.resolve()}, not the one under test")
        log(f"index under test: {index}")


@dataclass
class Ran:
    code: int
    out: str
    seconds: float
    timed_out: bool = False


def run(argv: list[str], env: dict[str, str], timeout: int, stdin: str | None = None) -> Ran:
    t = time.monotonic()
    try:
        p = subprocess.run(argv, env=env, input=stdin, capture_output=True, text=True, errors="replace", timeout=timeout)
        return Ran(p.returncode, plain(p.stdout + p.stderr), time.monotonic() - t)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return Ran(124, plain(out) + f"\n[lci] timed out after {timeout} s", time.monotonic() - t, True)


# ── probes: what a scenario needs of the computer, checked first ────


def probe(need: str, home: Home | None) -> str | None:
    """None when there; else why not."""
    if need == "kvm":
        if not os.path.exists("/dev/kvm"):
            return "no /dev/kvm"
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            return "/dev/kvm is not this user's (a udev rule gives it: README.md)"
        return None
    if need == "luban-os":
        return None if home and home.programs.luban_os else "no luban-os among the programs under test"
    if need == "xdesk":
        return None if home and home.programs.xdesk else "no xdesk among the programs under test"
    if need == "network":
        try:
            socket.create_connection(("github.com", 443), timeout=10).close()
            return None
        except OSError as e:
            return f"github.com is not reachable ({e})"
    return f"unknown need '{need}'"


# ── a SOCKS5 proxy for the scenarios that want one (no auth, CONNECT) ─


class Socks5:
    """socks5h on 127.0.0.1:<port>: names resolved here, as a proxy's exit does."""

    def __init__(self) -> None:
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(64)
        self.port = self.server.getsockname()[1]
        self.connections = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def url(self) -> str:
        return f"socks5h://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        while True:
            c, _ = self.server.accept()
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c: socket.socket) -> None:
        try:
            n = c.recv(2)[1]
            c.recv(n)
            c.sendall(b"\x05\x00")
            head = c.recv(4)
            kind = head[3]
            if kind == 1:
                host = socket.inet_ntoa(c.recv(4))
            elif kind == 3:
                host = c.recv(c.recv(1)[0]).decode()
            else:
                host = socket.inet_ntop(socket.AF_INET6, c.recv(16))
            port = int.from_bytes(c.recv(2), "big")
            up = self._connect(host, port)
            self.connections += 1
            c.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
            for a, b in ((c, up), (up, c)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()
        except Exception:  # noqa: BLE001 -- a refused client is its own business
            c.close()

    @staticmethod
    def _connect(host: str, port: int) -> socket.socket:
        """Out directly, or through the computer's own HTTP proxy when it has
        one (https_proxy): the proxy's exit is wherever this computer's is."""
        via = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""
        m = re.match(r"^http://([^:/]+):(\d+)/?$", via)
        if not m:
            return socket.create_connection((host, port), timeout=30)
        up = socket.create_connection((m.group(1), int(m.group(2))), timeout=30)
        up.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        answer = b""
        while b"\r\n\r\n" not in answer:
            chunk = up.recv(4096)
            if not chunk:
                break
            answer += chunk
        if not answer.split(b"\r\n", 1)[0].split(b" ")[1:2] == [b"200"]:
            up.close()
            raise OSError(f"{via} refused CONNECT {host}:{port}")
        return up

    @staticmethod
    def _pipe(a: socket.socket, b: socket.socket) -> None:
        try:
            while d := a.recv(65536):
                b.sendall(d)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


# ── fixtures and scenarios ──────────────────────────────────────────


@dataclass
class Scenario:
    id: str
    layer: str
    what: str
    file: Path
    covers: list[str]
    needs: list[str]
    systems: list[str]
    steps: list[dict]
    pending: str = ""
    budget: dict = field(default_factory=dict)


def load_scenarios(layer: str) -> list[Scenario]:
    out = []
    for f in sorted((ROOT / "scenarios" / layer).glob("*.toml")):
        d = tomllib.loads(f.read_text())
        out.append(Scenario(d["id"], layer, d["what"], f, d.get("covers", []), d.get("needs", []),
                            d.get("systems", [None]), d.get("step", []), d.get("pending", ""), d.get("budget", {})))
    return out


class Systems:
    """The systems scenarios run in, made once a run (fixtures.toml)."""

    def __init__(self, home: Home, proxy: Socks5 | None):
        self.home = home
        self.proxy = proxy
        self.fixtures = tomllib.loads((ROOT / "fixtures.toml").read_text())
        self.made: dict[str, str | None] = {}

    def get(self, fixture: str) -> tuple[str | None, str]:
        """(name, why not) -- the system for `fixture`, made the first time."""
        if fixture in self.made:
            return fixture, self.made[fixture] or ""
        spec = self.fixtures.get(fixture)
        if spec is None:
            self.made[fixture] = f"no fixture '{fixture}' in fixtures.toml"
            return None, self.made[fixture]
        args = [a.replace("{proxy}", self.proxy.url() if self.proxy else "") for a in spec["new"]]
        log(f"system {fixture}: luban new {fixture} {' '.join(args)}")
        r = self.home.sh(" ".join(["luban", "new", fixture, "-y", *map(shq, args)]), timeout=spec.get("timeout", 1800))
        (self.home.work / "logs").mkdir(exist_ok=True)
        (self.home.work / "logs" / f"new-{fixture}.log").write_text(r.out)
        self.made[fixture] = None if r.code == 0 else f"luban new {fixture}: exit {r.code}: {r.out.strip()[-400:]}"
        for c in spec.get("then", []):
            if self.made[fixture]:
                break
            rr = self.home.sh(c.replace("{system}", fixture), timeout=900)
            if rr.code != 0:
                self.made[fixture] = f"{c}: exit {rr.code}: {rr.out.strip()[-400:]}"
        return fixture, self.made[fixture] or ""

    def user_of(self, fixture: str) -> str:
        return self.fixtures.get(fixture, {}).get("user", "lubanoser")


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


@dataclass
class Result:
    scenario: str
    system: str | None
    status: str           # passed | failed | known (a named failure) | not-run
    why: str = ""
    seconds: float = 0.0
    steps: list[dict] = field(default_factory=list)
    measures: dict = field(default_factory=dict)


def expand(text: str, ctx: dict[str, str]) -> str:
    for k, v in ctx.items():
        text = text.replace("{" + k + "}", v)
    return text


def check(step: dict, r: Ran) -> list[str]:
    """What the step's expectations say of what it did."""
    problems = []
    want = step.get("exit", 0)
    if want == "nonzero":
        if r.code == 0:
            problems.append("exit 0, expected a refusal")
    elif r.code != want:
        problems.append(f"exit {r.code}, expected {want}")
    for s in step.get("has", []):
        if s not in r.out:
            problems.append(f"output lacks {s!r}")
    for s in step.get("lacks", []):
        if s in r.out:
            problems.append(f"output has {s!r}")
    for pattern in step.get("matches", []):
        if not re.search(pattern, r.out, re.M):
            problems.append(f"output does not match /{pattern}/")
    if (lines := step.get("max_lines")) is not None:
        n = len([l for l in r.out.splitlines() if l.strip()])
        if n > int(lines):
            problems.append(f"{n} lines of output, more than {lines}")
    return problems


def dotted(doc, path: str):
    for part in path.split("."):
        if isinstance(doc, list) and part.isdigit() and int(part) < len(doc):
            doc = doc[int(part)]
        elif isinstance(doc, dict) and part in doc:
            doc = doc[part]
        else:
            return None
    return doc


def limits(step: dict, ctx: dict[str, str], measures: dict) -> list[str]:
    """`read` a JSON file the step wrote; each of `limits` (a dotted path: the
    most it may be) is measured and held to it; `floors` the least."""
    if "read" not in step:
        return []
    f = Path(expand(step["read"], ctx))
    try:
        doc = json.loads(f.read_text())
    except (OSError, ValueError) as e:
        return [f"cannot read {f}: {e}"]
    problems = []
    for kind, table in (("limit", step.get("limits", {})), ("floor", step.get("floors", {}))):
        for path, bound in table.items():
            got = dotted(doc, path)
            if not isinstance(got, (int, float)):
                problems.append(f"{path} is not a number in {f.name} ({got!r})")
                continue
            measures[path] = got
            if kind == "limit" and got > bound:
                problems.append(f"{path} is {got}, over its limit of {bound}")
            if kind == "floor" and got < bound:
                problems.append(f"{path} is {got}, under its floor of {bound}")
    return problems


def run_step(home: Home, systems: Systems, system: str | None, step: dict, ctx: dict[str, str]) -> Ran:
    timeout = int(step.get("timeout", 600))
    command = expand(step["run"], ctx)
    where = step.get("in", "system" if system else "host")
    if where == "host":
        return home.sh(command, timeout)
    if where == "luban-os":
        script = home.work / f"script-{abs(hash(command))}.txt"
        script.write_text(expand(step["script"], ctx) if "script" in step else command)
        argv = f"luban-os --headless --script {shq(str(script))} " + expand(step.get("args", f"--attach {system}"), ctx)
        return home.sh(argv, timeout)
    user = step.get("user", systems.user_of(system or ""))
    if user == "{user}":
        user = systems.user_of(system or "")
    return home.sh(f"luban run {system} --user {user} -- sh -lc {shq(command)}", timeout, step.get("stdin"))


def run_scenario(sc: Scenario, system: str | None, home: Home, systems: Systems, keep_going: bool) -> Result:
    t = time.monotonic()
    res = Result(sc.id, system, "passed")
    if sc.pending:
        res.status, res.why = "not-run", f"pending: {sc.pending}"
        return res
    for need in sc.needs:
        if (why := probe(need, home)) is not None:
            res.status, res.why = "failed", f"probe '{need}' failed: {why}"
            return res
    ctx = {"system": system or "", "home": str(home.dir), "work": str(home.work), "proxy": systems.proxy.url() if systems.proxy else "",
           "user": systems.user_of(system or "") if system else ""}
    if system:
        name, why = systems.get(system)
        if why:
            res.status, res.why = "failed", why
            return res
    for i, step in enumerate(sc.steps, 1):
        r = run_step(home, systems, system, step, ctx)
        problems = check(step, r) + (limits(step, ctx, res.measures) if r.code == 0 else [])
        if "measure" in step:
            # The command's own measure (`measure_from`: a pattern whose group
            # is milliseconds it printed) -- a machine's boot is not the work.
            took = r.seconds
            if pattern := step.get("measure_from"):
                if m := re.search(pattern, r.out):
                    took = int(m.group(1)) / 1000
                else:
                    problems.append(f"no /{pattern}/ in the output to measure")
            res.measures[step["measure"]] = round(took, 3)
        res.steps.append({"n": i, "run": expand(step["run"], ctx), "exit": r.code, "seconds": round(r.seconds, 2),
                          "problems": problems, "out": r.out[-2000:]})
        label = f"step {i} ({step.get('what', step['run'])[:80]})"
        if problems and step.get("known"):
            # A failure someone has named (an issue, a fix not released yet):
            # said as known, not hidden, and not a failure of the run.
            if res.status == "passed":
                res.status = "known"
            res.why = (res.why + "; " if res.why else "") + f"{label}: known ({step['known']}): " + "; ".join(problems)
            continue
        if not problems and step.get("known"):
            res.why = (res.why + "; " if res.why else "") + f"{label} passes: its known failure ({step['known']}) is gone; drop `known`"
        if problems:
            res.status = "failed"
            res.why = (res.why + "; " if res.why else "") + f"{label}: " + "; ".join(problems)
            if not keep_going:
                break
    for name, limit in sc.budget.items():
        got = res.measures.get(name)
        if got is not None and got > float(limit):
            res.status = "failed"
            res.why = (res.why + "; " if res.why else "") + f"{name} took {got:.2f} s, over its budget of {limit} s"
    res.seconds = round(time.monotonic() - t, 2)
    return res


# ── the report ──────────────────────────────────────────────────────


def report(work: Path, layer: str, programs: Programs, results: list[Result]) -> int:
    counts = {s: sum(r.status == s for r in results) for s in ("passed", "failed", "known", "not-run")}
    doc = {"layer": layer, "version": programs.version, "counts": counts,
           "results": [r.__dict__ for r in results]}
    (work / "report.json").write_text(json.dumps(doc, indent=2))
    lines = [f"## luban-os-ci {layer}{' · xlings ' + programs.version if programs.version else ''}", "",
             f"{counts['passed']} passed · {counts['failed']} failed · {counts['known']} known · {counts['not-run']} not run", "",
             "| scenario | system | result | seconds | why |", "|---|---|---|---|---|"]
    for r in results:
        mark = {"passed": "✓", "failed": "✗", "known": "!", "not-run": "–"}[r.status]
        lines.append(f"| {r.scenario} | {r.system or ''} | {mark} {r.status} | {r.seconds} | {r.why.replace('|', '/')[:300]} |")
    md = "\n".join(lines) + "\n"
    (work / "report.md").write_text(md)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(md)
    print(md)
    return 0 if counts["failed"] == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="run.sh", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("layer", choices=LAYERS)
    ap.add_argument("only", nargs="*", help="scenario ids or globs (default: all of the layer)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--release", default=None, help="the released xlings to test: a version, or latest (default)")
    src.add_argument("--bin", type=Path, help="a directory of built xlings, luban (luban-os, xdesk) to test")
    ap.add_argument("--index", help="the index under test: a checkout, or a branch of openxlings/xim-pkgindex")
    ap.add_argument("--work", type=Path, default=Path(os.environ.get("LCI_WORK", ROOT / "work")))
    ap.add_argument("--system", action="append", default=[], help="only in this fixture (repeatable)")
    ap.add_argument("--setup", action="append", default=[],
                    help="a command run in the home before any scenario (repeatable): a computer's own preparation")
    ap.add_argument("--keep-going", action="store_true", help="a failed step does not end its scenario")
    ap.add_argument("--list", action="store_true", help="what would run, and nothing run")
    a = ap.parse_args()

    scenarios = [s for s in load_scenarios(a.layer)
                 if not a.only or any(fnmatch.fnmatch(s.id, o) or s.id == o for o in a.only)]
    if a.list:
        for s in scenarios:
            print(f"{s.id:8} {', '.join(x for x in s.systems if x) or 'host':28} {s.what}"
                  + (f"  [pending: {s.pending}]" if s.pending else ""))
        return 0
    if not scenarios:
        raise SystemExit(f"lci: no {a.layer} scenario matches {a.only}")
    run_id = time.strftime("%Y%m%d-%H%M%S")
    work = (a.work / f"{a.layer}-{run_id}").resolve()
    work.mkdir(parents=True)
    programs = directory_programs(a.bin) if a.bin else release_programs(a.release or "latest", a.work / "releases")
    log(f"testing xlings {programs.version or programs.xlings} in {work}")
    home = Home(work, programs, a.index, a.setup)
    proxy = Socks5() if any("proxy" in s.needs for s in scenarios) else None
    for s in scenarios:
        s.needs = [n for n in s.needs if n != "proxy"]
    systems = Systems(home, proxy)
    results = []
    for s in scenarios:
        for system in s.systems:
            if a.system and system not in a.system:
                continue
            log(f"{s.id} {system or ''}: {s.what}")
            r = run_scenario(s, system, home, systems, a.keep_going)
            log(f"  {r.status}{': ' + r.why if r.why else ''} ({r.seconds} s)")
            results.append(r)
    for name in [n for n, why in systems.made.items() if not why]:
        home.sh(f"luban stop {name}", timeout=120)
    return report(work, a.layer, programs, results)


if __name__ == "__main__":
    sys.exit(main())
