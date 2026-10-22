# luban-os-ci

Luban's own CI: what a person meets in a Luban system, checked the way they
meet it -- `luban new`, a machine booting, `xlings` inside it, the desktop in
the Luban OS window, a released version installed and upgraded. It drives the
programs under test (`luban`, `xlings`, `luban-os --headless --script`,
`xdesk msg`) and links nothing of xlings: what it can see is what a person
can.

Design: xlings `.agents/docs/2026-10-21-luban-os-ecosystem-usability-design.md`
§9 (layers, scenarios, matrix, triggers). L0 and L1 (unit tests, sandbox
end-to-end tests) stay in xlings, beside the code.

## Running it

The same on a computer as in CI:

```bash
./run.sh L2                                   # the newest release, every L2 scenario
./run.sh L2 L2-03 --system core-machine       # one scenario, in one system
./run.sh L4 --release 2026.10.22.1            # a released version
./run.sh L2 --bin ../xlings/target/x86_64-linux-musl/<id>/bin   # a build (static xlings and luban)
./run.sh L3 --index ../xim-pkgindex           # an index under test (a checkout or a branch)
./run.sh L2 --list                            # what would run
```

Needs `python3` (3.11+), `git` and, for machines, `/dev/kvm` this user can
open (on a hosted runner: a udev rule, see `.github/workflows/layers.yml`).
A run works in `work/<layer>-<time>/` (`--work`): an xlings home of its own
(`home/`, nothing of yours is read or written), the systems it made, their
logs, and its report -- `report.md`, `report.json`.

`--setup '<command>'` runs a command in that home before the scenarios: a
computer's own preparation (on a host whose `/usr/bin/bwrap` is not usable:
`--setup 'xlings install -y xim:bwrap'`).

## What a run says

Each scenario in each of its systems is `passed`, `failed` (and why: the
step, what it expected, the end of its output) or `not run` (and why: a
scenario `pending` on something not released yet). A scenario whose `needs`
are not met -- no `/dev/kvm`, no `luban-os` among the programs -- **fails**,
naming the probe: a run that tested nothing never shows as passed.

## Layers

| Layer | What | Where |
|---|---|---|
| L2 | xlings in a system (`scenarios/L2`): searching, installing for oneself and for everyone, `update` needing sudo, users with versions of their own, mcpp, doctor, the host installing into it, persistence, the network, shells, upgrades, the agent workspace's approvals | machines (KVM) |
| L3 | every package's declared checks (`luban status --check`, outside and in) | machines |
| L4 | the desktop: the Luban OS window draws it, the launcher lists, Falkon opens a page, sound plays | machines, `luban-os` |
| L5 | budgets: files over virtio-fs and 9p, an idle desktop's frames; CI compares, a reference computer accepts | machines |
| L6 | released versions: made from, exported to an ISO that boots | the release |

## Scenarios

A scenario is a file, `scenarios/<layer>/<nn>-<name>.toml`:

```toml
id = "L2-03"
what = "updating the index affects everyone: refused without sudo ..."
covers = ["LCI-L2-SUDO"]           # requirements.toml
needs = ["kvm"]                    # probed first: kvm, luban-os, xdesk, network, proxy
systems = ["core-machine"]         # fixtures.toml; run once in each
# pending = "why it does not run yet"

[[step]]
run = "xlings update"              # in the system, as its user (`luban run`)
exit = "nonzero"                   # or a number (default 0)
has = ["sudo xlings update"]       # in its output
lacks = ["[warn]"]
# in = "host" | "luban-os" (script = "...", args = "--attach {system}")
# user = "root" | "alice" ...; timeout = 600; measure = "<name>" (seconds the step took,
#   or measure_from = "<pattern>": milliseconds the command printed); max_lines = 3
# read = "{work}/state.json"; limits = { "perf.frames" = 120 }; floors = {...}
```

`{system}`, `{home}`, `{work}`, `{proxy}` and `{user}` are replaced in `run`
and `script`. `[budget]` holds a scenario's measured steps to seconds.
`fixtures.toml` names the systems: how `luban new` makes each, made once a
run, the first time a scenario needs it.

## CI

`.github/workflows/layers.yml` runs a layer on GitHub's hosted runners: on a
dispatch (from xlings' CI with its build, or by hand), every day with the
newest release and xim-pkgindex main (a failure opens an issue), and before a
release (L6). Each job first probes what it needs and fails when it is not
there. What hosted runners cannot run is listed in the report with the reason
and what covers it instead (design §9.5).
