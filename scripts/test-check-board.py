"""Negative tests for scripts/check-board.py: every rule must actually fire.

A checker that only ever prints OK is indistinguishable from no checker. Each
case writes a throwaway board.toml, points the script at it through a temporary
device directory, and asserts the expected message appears.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEVICE = Path(r"E:\one plus\tools\UniXNU-erhai")
GOOD = (DEVICE / "board.toml").read_text(encoding="utf-8")


CASES = [
    ("source_only with prelink",
     GOOD.replace("source_only = false", "source_only = true"),
     "source_only and [kernel.prelink] cannot be used together"),
    ("seed without chip_id",
     GOOD.replace('chip_id = "0x8011"\n', ""),
     "unique_chip_id and chip_id must be supplied together"),
    ("chip_id out of range",
     GOOD.replace('chip_id = "0x8011"', 'chip_id = "0x180110011"'),
     "chip_id must be nonzero and fit 32 bits"),
    ("seed is a typo sentinel",
     GOOD.replace('unique_chip_id = "required"', 'unique_chip_id = "requird"'),
     "neither a number nor"),
    ("archive that does not exist",
     GOOD.replace('  "build-boot-progress.py",\n', '  "build-no-such-thing.py",\n'),
     "is not in the build tree"),
    ("payload too small for a prelinked kernel",
     GOOD.replace("size_mib = 192", "size_mib = 64"),
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
            for line in (result.stdout + result.stderr).splitlines()[:6]:
                print(f"        {line}")
        return hit


def main():
    print(f"check-board.py negative tests ({len(CASES)} cases)")
    ok = all([run_case(*case) for case in CASES])
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
