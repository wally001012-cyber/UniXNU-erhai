#!/usr/bin/env python3
"""Reject a board.toml the build would fail on, before the macOS runner is used.

Everything here is a rule the tooling enforces somewhere deep in the build, so
catching it up front turns a 9-minute red run into a 2-second red step. The
checks are the ones that have actually bitten this port:

  * [kernel] source_only together with [kernel.prelink] — export_source refuses
    the pair, and a source-only kernel carries no Apple drivers.
  * [payload.adt] unique_chip_id without chip_id — make-erhai-adt.py requires
    both, and both nonzero.
  * archives listed here but absent from the build tree, which shows up as a
    "no such script" an hour into the link.
  * payload size_mib smaller than the inputs; the packer does report this, but
    only after the kernel is built.
"""
import sys
import tomllib
from pathlib import Path

DEVICE = Path(__file__).resolve().parents[1]
BOARD = DEVICE / 'board.toml'


def fail(msg):
    print(f'FAIL: {msg}')
    return 1


def main():
    tree = Path(sys.argv[1]) if len(sys.argv) > 1 else DEVICE / '.cache'
    if not BOARD.is_file():
        return fail(f'{BOARD} missing')
    board = tomllib.loads(BOARD.read_text())

    problems = 0

    kernel = board.get('kernel', {})
    if kernel.get('source_only') and kernel.get('prelink'):
        problems += fail('[kernel] source_only and [kernel.prelink] cannot be used together')

    adt = board.get('payload', {}).get('adt', {})
    seed, chip = adt.get('unique_chip_id'), adt.get('chip_id')
    if (seed is None) != (chip is None):
        problems += fail('[payload.adt] unique_chip_id and chip_id must be supplied together')
    # "required" is the sentinel payload.py understands: it then takes the seed
    # from --unique-chip-id / UNIXNU_UNIQUE_CHIP_ID. Any literal value here
    # shadows that secret, so only the sentinel and real hex are accepted.
    if isinstance(seed, str) and seed != 'required':
        try:
            number = int(seed, 0)
        except ValueError:
            problems += fail(f'[payload.adt] unique_chip_id is neither a number nor '
                             f'"required": {seed!r}')
        else:
            if not 0 < number < 1 << 64:
                problems += fail(f'[payload.adt] unique_chip_id must be nonzero and fit '
                                 f'64 bits, got {seed!r}')
    if chip is not None:
        try:
            number = int(chip, 0)
        except (TypeError, ValueError):
            problems += fail(f'[payload.adt] chip_id is not a number: {chip!r}')
        else:
            if not 0 < number < 1 << 32:
                problems += fail(f'[payload.adt] chip_id must be nonzero and fit 32 bits, '
                                 f'got {chip!r}')

    # The generator only emits unique-chip-id when the packer forwards it, which
    # it does from the CLI/env, not from board.toml. Say so loudly instead of
    # letting a silent zero seed through.
    if seed:
        generator = DEVICE / 'scripts/make-erhai-adt.py'
        if not generator.is_file():
            problems += fail(f'{generator} is missing; the ADT cannot be generated at all')
        elif '--unique-chip-id' not in generator.read_text(encoding='utf-8'):
            problems += fail('scripts/make-erhai-adt.py does not accept --unique-chip-id')

    # Archive scripts must exist in the prepared tree. Newest first: after a
    # patch change prepare() writes a new upstream-<rev>-<fingerprint> directory
    # and the stale ones stay behind, so picking the alphabetically first one
    # reports a phantom failure.
    def prepared_trees():
        base = tree if tree.is_dir() else DEVICE / '.cache'
        found = [p for p in base.glob('upstream-*') if (p / 'scripts').is_dir()]
        return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)

    trees = prepared_trees()
    if trees:
        scripts = {p.name for p in (trees[0] / 'scripts').glob('*.py')}
        print(f'tree: {trees[0].name} ({len(scripts)} scripts)')
        for entry in kernel.get('archives', []):
            name = entry.split()[0]
            if name not in scripts:
                problems += fail(f'[kernel] archives: {name} is not in the build tree '
                                 f'({trees[0].name})')

    payload = board.get('payload', {})
    size_mib = payload.get('size_mib')
    if not isinstance(size_mib, int) or size_mib <= 0:
        problems += fail(f'[payload] size_mib must be a positive integer, got {size_mib!r}')
    elif kernel.get('prelink') and size_mib < 128:
        # A prelinked kernel is ~67 MiB before the ADT and trust cache land.
        problems += fail(f'[payload] size_mib={size_mib} is too small for a prelinked kernel')

    if problems:
        return 1
    print(f'board.toml OK: source_only={kernel.get("source_only", False)}, '
          f'prelink={"yes" if kernel.get("prelink") else "no"}, '
          f'chip_id={chip}, seed={"set" if seed else "MISSING"}, '
          f'size_mib={size_mib}, archives={len(kernel.get("archives", []))}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
