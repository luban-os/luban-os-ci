"""The scenarios are well formed: each has an id that matches its layer and
file, says what it checks, covers known requirements, names known fixtures
and needs, and every requirement is covered. Runs anywhere (no machine)."""
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEEDS = {"kvm", "luban-os", "xdesk", "network", "proxy"}


def main() -> int:
    reqs = tomllib.loads((ROOT / "requirements.toml").read_text())
    fixtures = tomllib.loads((ROOT / "fixtures.toml").read_text())
    covered, bad, ids = set(), [], set()
    for f in sorted((ROOT / "scenarios").glob("L*/*.toml")):
        d = tomllib.loads(f.read_text())
        where = f"{f.relative_to(ROOT)}"
        layer = f.parent.name
        if not re.fullmatch(rf"{layer}-\d\d", d.get("id", "")):
            bad.append(f"{where}: id {d.get('id')!r} is not {layer}-NN")
        if d.get("id") in ids:
            bad.append(f"{where}: id {d['id']} twice")
        ids.add(d.get("id"))
        if not f.name.startswith(d.get("id", "x")[3:5] + "-"):
            bad.append(f"{where}: its file does not start with its number")
        if not d.get("what"):
            bad.append(f"{where}: says not what it checks")
        for c in d.get("covers", []) or [None]:
            if c not in reqs:
                bad.append(f"{where}: covers {c!r}, not in requirements.toml")
            covered.add(c)
        for s in d.get("systems", []):
            if s not in fixtures:
                bad.append(f"{where}: system {s!r} is not in fixtures.toml")
        for n in d.get("needs", []):
            if n not in NEEDS:
                bad.append(f"{where}: needs {n!r}, which no probe knows")
        if not d.get("pending") and not d.get("step"):
            bad.append(f"{where}: no steps and not pending")
        for i, st in enumerate(d.get("step", []), 1):
            if "run" not in st:
                bad.append(f"{where}: step {i} runs nothing")
            if st.get("in", "system") not in ("system", "host", "luban-os"):
                bad.append(f"{where}: step {i} is in {st['in']!r}")
    bad += [f"requirements.toml: {r} is covered by no scenario" for r in reqs if r not in covered]
    for b in bad:
        print(b)
    print(f"scenarios: {len(ids)}, requirements: {len(reqs)}, problems: {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
