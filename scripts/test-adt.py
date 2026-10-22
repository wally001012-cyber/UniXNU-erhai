#!/usr/bin/env python3
"""Contract tests for the ADT that make-erhai-adt.py emits.

These run on any host: no runner, no hardware, no fdtget. The tree is decoded
straight out of make_tree(), so a change in the generator that breaks the
platform expert's expectations fails here instead of on the tablet.

The workflow runs the first two cases explicitly before the macOS runner is
spent; the live-DTB case skips unless the dump and fdtget are both present.
"""
import importlib.util
import shutil
import struct
import subprocess
import unittest
import zlib
from pathlib import Path

DEVICE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('adt', Path(__file__).with_name('make-erhai-adt.py'))
adt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adt)

SEED = bytes(range(64))
OWNED = 0x10000000


def decode(data):
    """Decode the flat ADT into {path: {property: bytes}}."""
    nodes = {}

    def parse(at, parent):
        prop_count, child_count = struct.unpack_from('<II', data, at)
        at += 8
        props = {}
        for _ in range(prop_count):
            name, size = struct.unpack_from('<32sI', data, at)
            at += 36
            key = name.rstrip(b'\0').decode()
            assert key not in props and at + size <= len(data)
            props[key] = data[at:at + size]
            at += (size + 3) & ~3
        path = parent + '/' + props['name'].rstrip(b'\0').decode()
        nodes[path] = props
        for _ in range(child_count):
            at = parse(at, path)
        return at

    assert parse(0, '') == len(data), 'the ADT has trailing bytes'
    return nodes


def tree(**kwargs):
    return decode(adt.make_tree(OWNED, SEED, **kwargs))


class AdtTest(unittest.TestCase):
    def test_tree_contract(self):
        """The skeleton every profile must carry, whatever the identity flags."""
        nodes = tree()
        root = '/device-tree'
        self.assertTrue({root + '/' + n for n in ('chosen', 'cpus', 'arm-io', 'defaults', 'options')}
                        <= nodes.keys())
        chosen = nodes[root + '/chosen']
        self.assertEqual(chosen['random-seed'], SEED)
        self.assertEqual(struct.unpack('<Q', chosen['darwinboot-memory-size']), (OWNED,))
        # Zero placeholders: DarwinBoot writes what it actually owns.
        self.assertEqual(chosen['dram-base'], bytes(8))
        self.assertEqual(chosen['dram-size'], bytes(8))
        self.assertEqual(chosen['crypto-hash-method'], b'sha2-384\0')
        self.assertEqual(list(nodes[root + '/chosen/memory-map']), ['name'])
        # One core by default, no psci without a second one.
        self.assertEqual(nodes[root + '/cpus/cpu0']['reg'], bytes(4))
        self.assertNotIn(root + '/cpus/cpu1', nodes)
        self.assertNotIn(root + '/psci', nodes)
        # The NVRAM proxy must be self-consistent or IODTNVRAM rejects it.
        bank = chosen['nvram-proxy-data']
        self.assertEqual(struct.unpack_from('<I', bank, 16)[0], zlib.adler32(bank[20:]))
        for at in (0, 32, 2096):
            header = bank[at:at + 16]
            checksum = sum(header[:1] + header[2:])
            while checksum > 255:
                checksum = (checksum & 255) + (checksum >> 8)
            self.assertEqual(header[1], checksum)

    def test_invalid_window(self):
        """Every out-of-range owned window must raise, not emit a bad tree."""
        for size in (0, 0x1000, 0x10001000, 0x20001000, 0x200000001):
            with self.assertRaises(ValueError, msg=f'{size:#x} should be rejected'):
                adt.make_tree(size, SEED)
        adt.make_tree(0x10000000, SEED)      # 256 MiB, the floor
        adt.make_tree(0x200000000, SEED)     # 8 GiB, the ceiling

    def test_seed_must_be_64_bytes(self):
        for seed in (b'', bytes(32), bytes(65)):
            with self.assertRaises(ValueError):
                adt.make_tree(OWNED, seed)

    def test_identity_pair_is_all_or_nothing(self):
        """unique-chip-id and chip-id only mean something together."""
        with self.assertRaises(ValueError):
            adt.make_tree(OWNED, SEED, unique_chip_id=0x45a697358b6)
        with self.assertRaises(ValueError):
            adt.make_tree(OWNED, SEED, chip_id=0x8011)
        with self.assertRaises(ValueError):
            adt.make_tree(OWNED, SEED, unique_chip_id=0, chip_id=0x8011)
        with self.assertRaises(ValueError):
            adt.make_tree(OWNED, SEED, unique_chip_id=1, chip_id=1 << 32)
        nodes = tree(unique_chip_id=0x45a697358b6, chip_id=0x8011)
        self.assertEqual(struct.unpack('<Q', nodes['/device-tree/chosen']['unique-chip-id']),
                         (0x45a697358b6,))
        self.assertEqual(struct.unpack('<I', nodes['/device-tree/chosen']['chip-id']), (0x8011,))

    def test_erhai_hardware_constants(self):
        """The GIC and bus numbers the live device tree reports."""
        nodes = tree()
        bus = struct.unpack('<3Q', nodes['/device-tree/arm-io']['ranges'])
        self.assertEqual(bus, (0, adt.ARMIO_BASE, adt.ARMIO_SIZE))
        reg = struct.unpack('<4Q', nodes['/device-tree/arm-io/intc']['reg'])
        # intc offsets are relative to the arm-io window.
        self.assertEqual((bus[1] + reg[0], bus[1] + reg[2]), (adt.GIC_DIST, adt.GIC_REDIST))
        self.assertEqual(reg[1], adt.GIC_DIST_SIZE)
        self.assertEqual(reg[3], adt.GIC_REDIST_SIZE)
        self.assertLessEqual(reg[2] + reg[3], bus[2])
        self.assertEqual(struct.unpack('<Q', nodes['/device-tree/arm-io/intc']['redistributor-stride']),
                         (adt.GIC_REDIST_STRIDE,))
        self.assertEqual(struct.unpack('<I', nodes['/device-tree/cpus/cpu0']['timebase-frequency']),
                         (adt.TIMEBASE,))

    def test_ipad_identity(self):
        """What sheng carries, and therefore what erhai must carry too."""
        nodes = tree(ipad_identity=True, unique_chip_id=0x45a697358b6, chip_id=0x8011)
        root = '/device-tree'
        self.assertEqual(nodes[root]['model'], b'iPad7,1\0')
        self.assertEqual(nodes[root]['target-type'], b'J120\0')
        self.assertEqual(nodes[root + '/product']['product-name'],
                         b'iPad Pro (12.9-inch) (2nd generation)\0')
        self.assertEqual(struct.unpack('<I', nodes[root + '/product']['artwork-device-subtype']),
                         (2732,))
        self.assertEqual(nodes[root + '/product']['compatible-device-fallback'], b'iPad6,7\0')
        self.assertEqual(struct.unpack('<I', nodes[root + '/chosen']['certificate-security-mode']), (1,))
        self.assertEqual(struct.unpack('<I', nodes[root + '/chosen']['effective-production-status-ap']), (0,))
        # No Home button on erhai, so no 'menu' entry.
        self.assertEqual(nodes[root + '/buttons']['button-names'], b'hold\0voldown\0volup\0')

    def test_memory_map_placeholders(self):
        """A declared Data ramdisk and trust cache need their leaves.

        node('memory-map', memory_map) passes the pairs as *properties*, the way
        the platform expert reads them, so they are checked as properties.
        """
        both = tree(packed_data_ramdisk=True, trust_cache=True)['/device-tree/chosen/memory-map']
        self.assertIn('PackedRAMDisk', both)
        self.assertIn('TrustCache', both)
        self.assertEqual(len(both['PackedRAMDisk']), 16)
        self.assertEqual(len(both['TrustCache']), 16)
        # PackedRAMDisk wins over RAMDisk when both flags are set.
        self.assertNotIn('RAMDisk', both)
        only_ramdisk = tree(data_ramdisk=True)['/device-tree/chosen/memory-map']
        self.assertIn('RAMDisk', only_ramdisk)
        self.assertNotIn('PackedRAMDisk', only_ramdisk)
        self.assertNotIn('TrustCache', only_ramdisk)
        # Without the flags the map carries nothing but its own name.
        self.assertEqual(list(tree()['/device-tree/chosen/memory-map']), ['name'])

    def test_cli_and_make_tree_agree(self):
        """main() must not apply its own rules on top of make_tree()."""
        import tempfile
        with tempfile.TemporaryDirectory() as name:
            out = Path(name) / 'x.adt'
            rc = adt.main(['--output', str(out), '--dram-size', '0x10000000',
                           '--owned-memory-size', '0x10000000', '--ipad-identity',
                           '--chip-id', '0x8011', '--unique-chip-id', '45a697358b6',
                           '--cpu-regs', '0,0x100'])
            self.assertEqual(rc, 0)
            nodes = decode(out.read_bytes())
            self.assertEqual(nodes['/device-tree']['model'], b'iPad7,1\0')
            self.assertIn('/device-tree/cpus/cpu1', nodes)
            self.assertEqual(struct.unpack('<I', nodes['/device-tree/cpus/cpu1']['reg']), (0x100,))

    def test_cpu_regs_reject_bad_layouts(self):
        """Reject malformed MPIDR lists, accept the real erhai topology."""
        for regs in ((), (0x100,), (0, 0), (0, 0x100, 0x100), (0, 0x101),
                     (0, 0x1000000), (0, -0x100)):
            with self.assertRaises(ValueError, msg=f'{regs!r} should be rejected'):
                adt.make_tree(OWNED, SEED, cpu_regs=regs)
        # The measured erhai topology: six cores in cluster 0, two at Aff2=1.
        # sheng's 0xffff bound would reject this, which is why it is 0xffffff.
        regs = (0, 0x100, 0x200, 0x300, 0x400, 0x500, 0x10000, 0x10100)
        nodes = tree(cpu_regs=regs)
        for index, reg in enumerate(regs):
            self.assertEqual(
                struct.unpack('<I', nodes[f'/device-tree/cpus/cpu{index}']['reg']), (reg,))
        self.assertIn('/device-tree/psci', nodes)
        self.assertNotIn('/device-tree/cpus/cpu8', nodes)

    def test_actual_board_resources(self):
        """Cross-check against the live FDT dump, when it and fdtget are here."""
        dtb = DEVICE / 'sysinfo/live.dtb'
        if not dtb.is_file() or shutil.which('fdtget') is None:
            self.skipTest('sysinfo/live.dtb or fdtget missing; the live cross-check needs the host')

        def cells(node, prop):
            output = subprocess.check_output(['fdtget', '-t', 'x', dtb, node, prop], text=True)
            return [int(s, 16) for s in output.split()]

        self.assertEqual(cells('/soc/interrupt-controller@16000000', 'reg'),
                         [adt.GIC_DIST, adt.GIC_DIST_SIZE, adt.GIC_REDIST, adt.GIC_REDIST_SIZE])
        self.assertEqual(cells('/soc/interrupt-controller@16000000', 'redistributor-stride'),
                         [0, adt.GIC_REDIST_STRIDE])
        self.assertEqual(cells('/soc/timer', 'clock-frequency'), [adt.TIMEBASE])
        self.assertEqual(cells('/cpus/cpu@0', 'reg'), [0, 0])


if __name__ == '__main__':
    unittest.main()
