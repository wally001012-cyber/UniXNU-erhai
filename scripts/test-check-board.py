"""Negative tests for scripts/check-board.py: every rule must actually fire.

A checker that only ever prints OK is indistinguishable from no checker. Each
case loads the real board.toml, mutates the parsed structure, writes it back and
asserts the expected message appears.

Mutating the parsed TOML instead of doing string replacements on the file is not
fussiness: the string version silently stopped testing anything the moment
board.toml changed (chip_id went from "0x8011" to "207", the replace() calls
matched nothing, the board stayed valid, and two cases reported FAIL while
actually proving nothing). A structural edit either applies or raises KeyError.

Everything is located relative to this file, so the suite runs unchanged on the
macOS runner and on a Windows host.
"""
import copy
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

DEVICE = Path(__file__).resolve().parents[1]
BASE = tomllib.loads((DEVICE / "board.toml").read_text(encoding="utf-8"))


def tomlize(value, prefix=""):
    """Serialise the subset of TOML board.toml uses, keeping types intact."""
    lines = []
    scalars = {k: v for k, v in value.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in value.items() if isinstance(v, dict)}
    for key, val in scalars.items():
        lines.append(f"{key} = {literal(val)}")
    for name, table in tables.items():
        path = f"{prefix}{name}"
        lines.append("")
        lines.append(f"[{path}]")
        nested_scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        nested_tables = {k: v for k, v in table.items() if isinstance(v, dict)}
        for key, val in nested_scalars.items():
            lines.append(f"{key} = {literal(val)}")
        for sub, subtable in nested_tables.items():
            lines.append("")
            lines.append(f"[{path}.{sub}]")
            for key, val in subtable.items():
                lines.append(f"{key} = {literal(val)}")
    return "\n".join(lines) + "\n"


def literal(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        inner = ",\n  ".join(literal(v) for v in value)
        return f"[\n  {inner},\n]"
    if isinstance(value, dict):
        # An inline table, which is how board.toml writes [[bootimg.dtb_node]]
        # entries; the real file uses that form and check-board.py only reads it
        # through tomllib, so the inline form parses identically.
        items = ", ".join(f"{k} = {literal(v)}" for k, v in value.items())
        return "{ " + items + " }"
    raise TypeError(f"cannot serialise {type(value)}")


def board(**mutations):
    """A copy of the real board with dotted-path changes applied.

    board(**{'payload.adt.chip_id': '0x180110011'}) sets that key. A value of
    DELETE removes the key, which is how the "missing field" cases are built.
    """
    data = copy.deepcopy(BASE)
    for path, value in mutations.items():
        parts = path.split(".")
        node = data
        for part in parts[:-1]:
            node = node[part]
        if value is DELETE:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value
    return tomlize(data)


class _Delete:
    def __repr__(self):
        return "<delete>"


DELETE = _Delete()

CASES = [
    ("source_only with prelink",
     board(**{"kernel.source_only": True}),
     "source_only and [kernel.prelink] cannot be used together"),
    ("seed without chip_id",
     board(**{"payload.adt.chip_id": DELETE}),
     "unique_chip_id and chip_id must be supplied together"),
    ("chip_id out of range",
     board(**{"payload.adt.chip_id": "0x180110011"}),
     "chip_id must be nonzero and fit 32 bits"),
    ("seed is a typo sentinel",
     board(**{"payload.adt.unique_chip_id": "requird"}),
     "neither a number nor"),
    ("archive that does not exist",
     board(**{"kernel.archives": ["build-no-such-thing.py"]}),
     "is not in the build tree"),
    ("payload too small for a prelinked kernel",
     board(**{"payload.size_mib": 64}),
     "too small for a prelinked kernel"),
]


def run_case(name, board_text, expect):
    with tempfile.TemporaryDirectory(prefix="boardcase-") as tmp:
        root = Path(tmp) / "device"
        (root / "scripts").mkdir(parents=True)
        (root / "configs").mkdir()
        (root / "board.toml").write_text(board_text, encoding="utf-8")
        shutil.copyfile(DEVICE / "scripts/check-board.py", root / "scripts/check-board.py")
        # check-board.py locates the ADT generator relative to itself, so the
        # case has to carry one too; otherwise every case dies at that check
        # before reaching the rule it is meant to exercise.
        shutil.copyfile(DEVICE / "scripts/make-erhai-adt.py",
                        root / "scripts/make-erhai-adt.py")
        # Give it an archive tree so the archives rule is reachable.
        tree = Path(tmp) / "tree" / "upstream-test"
        (tree / "scripts").mkdir(parents=True)
        for name_ in ("build-corecrypto-14.py", "build-arm-platform.py",
                      "build-libpthread-kernel.py", "build-boot-progress.py"):
            (tree / "scripts" / name_).write_text("", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(root / "scripts/check-board.py"), str(Path(tmp) / "tree")],
            capture_output=True, text=True)
        hit = expect in result.stdout
        print(f"  {'PASS' if hit else 'FAIL'}  {name}")
        if not hit:
            print(f"        rc={result.returncode}")
            for line in (result.stdout + result.stderr).splitlines()[:8]:
                print(f"        {line}")
        return hit


def main():
    print(f"check-board.py negative tests ({len(CASES)} cases)")
    ok = all([run_case(*case) for case in CASES])
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
