#!/usr/bin/env python3
"""Apple-format device tree for OnePlus Pad 2 Pro (erhai / OPD2413), first boot.

Ported from UniXNU-sheng scripts/make-sheng-adt.py (the only board that has
reached an iOS home screen) with erhai's own measured constants:

  GICv3 distributor   0x16000000 / 0x10000     /proc/iomem "GICD"
  GICv3 redistributor 0x16080000 / 0x200000    /proc/iomem "GICR"
  APSS arm-io window  0x16000000 / 0x02000000  MemoryMapLib "APSS_HM"
  UFS host            0x1d84000  (not used by this profile)
  timebase            19200000   CNTFRQ_EL0 on hardware
  DRAM base           0x80000000 (hypervisor carveout start; /proc/iomem)
  panel               3392x2400, no Home button

Everything that is *identity* rather than hardware is copied from sheng on
purpose: the target image is the same unchanged iPad7,1 / 21H461 restore, so the
platform expert has to believe it is an iPad Pro (12.9-inch) 2nd generation.

Contents of the tree follow UniXNU docs/porting/device-tree.md:
  /chosen            random-seed, NVRAM CHRP proxy, dram placeholders,
                     darwinboot-memory-size, crypto-hash-method, identity
  /chosen/memory-map RAMDisk / PackedRAMDisk / TrustCache placeholders
  /cpus              cpuN with state/reg/device_type/timebase-frequency
  /psci              only when more than one core is published
  /defaults /options required by the platform expert
  /arm-io            bus node with ranges and the intc child
  /product /buttons  MobileGestalt and BSPlatform inputs

make_tree() is the single source of truth for the bytes; main() only parses
arguments and forwards them. scripts/test-adt.py decodes make_tree() output
directly, so the contract test needs no runner and no hardware.
"""
import argparse
import os
import struct
import sys
import zlib
from pathlib import Path

DRAM_BASE = 0x80000000
ARMIO_BASE = 0x16000000          # APSS_HM
ARMIO_SIZE = 0x02000000
GIC_DIST = 0x16000000
GIC_DIST_SIZE = 0x10000
GIC_REDIST = 0x16080000
GIC_REDIST_SIZE = 0x200000
GIC_REDIST_STRIDE = 0x40000
TIMEBASE = 19200000

OWNED_MIN = 0x10000000           # 256 MiB
OWNED_MAX = 0x200000000          # 8 GiB
OWNED_ALIGN = 0x200000           # 2 MiB
SEED_BYTES = 64


def node(name, properties=(), children=()):
    properties = [('name', name.encode() + b'\0'), *properties]
    out = struct.pack('<II', len(properties), len(children))
    for key, data in properties:
        if len(key.encode()) > 31:
            raise ValueError(f'ADT property name exceeds 31 bytes: {key}')
        out += struct.pack('<32sI', key.encode(), len(data))
        out += data + bytes(-len(data) % 4)
    return out + b''.join(children)


def empty_nvram_proxy():
    """A fresh volatile CHRP bank so IODTNVRAM can publish without a controller."""
    def header(name, size):
        raw = bytearray(struct.pack('<BBH12s', 0x70, 0, size // 16, name))
        checksum = sum(raw)
        while checksum > 255:
            checksum = (checksum & 255) + (checksum >> 8)
        raw[1] = checksum
        return raw

    image = bytearray(8192)
    image[:16] = header(b'nvram', 32)
    image[32:48] = header(b'common', 16 + 2048)
    free_at = 32 + 16 + 2048
    image[free_at:free_at + 16] = header(b'free', len(image) - free_at)
    struct.pack_into('<I', image, 16, zlib.adler32(image[20:]))
    return [('nvram-bank-size', struct.pack('<I', len(image))),
            ('nvram-bank-count', struct.pack('<I', 1)),
            ('nvram-current-bank', struct.pack('<I', 0)),
            ('nvram-proxy-data', bytes(image))]


def make_tree(memory_size, random_seed, *, dram_size=0x10000000,
              unique_chip_id=None, chip_id=None, ipad_identity=False,
              cpu_regs=(0,), psci_method='smc', packed_data_ramdisk=False,
              data_ramdisk=False, trust_cache=False,
              root_match_volume_uuid=None):
    """Build the ADT as one node tree.

    memory_size is the contiguous window DarwinBoot hands to XNU; it is
    validated here so both the CLI and the contract test see the same rule.
    """
    if not OWNED_MIN <= memory_size <= OWNED_MAX or memory_size % OWNED_ALIGN:
        raise ValueError('owned memory must be 256 MiB..8 GiB, 2 MiB aligned')
    if len(random_seed) != SEED_BYTES:
        raise ValueError('random seed must be 64 bytes')
    if (unique_chip_id is None) != (chip_id is None):
        raise ValueError('unique-chip-id and chip-id must be supplied together')
    if unique_chip_id is not None and not (0 < unique_chip_id < 1 << 64 and 0 < chip_id < 1 << 32):
        raise ValueError('unique-chip-id must fit 8 bytes and chip-id 4 bytes, both nonzero')
    cpu_regs = list(cpu_regs)
    if (not cpu_regs or cpu_regs[0] != 0 or len(set(cpu_regs)) != len(cpu_regs) or len(cpu_regs) > 8
            or any(r < 0 or r > 0xffff or r & 0xff for r in cpu_regs)):
        raise ValueError('cpu-regs: up to eight distinct Affinity1-numbered cores, boot CPU 0 first')

    cores = [node('cpu%d' % index,
                  [('state', b'running\0' if index == 0 else b'waiting\0'),
                   ('reg', struct.pack('<I', reg)),
                   ('device_type', b'cpu\0'),
                   # SM8750 ships Cortex-A725/A520 in one cluster layout; the
                   # platform expert only needs a usable string here.
                   ('compatible', b'arm,armv8\0'),
                   ('timebase-frequency', struct.pack('<I', TIMEBASE))])
             for index, reg in enumerate(cpu_regs)]

    chosen = [('random-seed', random_seed)]
    if unique_chip_id is not None:
        chosen += [('unique-chip-id', struct.pack('<Q', unique_chip_id)),
                   ('chip-id', struct.pack('<I', chip_id))]
    chosen += [('osenvironment', b'main\0')]
    if ipad_identity:
        # dev-fused, undemoted board tuple (0,0,1,1) as MobileGestalt reads it;
        # mobileactivationd then takes its native Hactivation branch.
        chosen += [('certificate-production-status', struct.pack('<I', 0)),
                   ('effective-production-status-ap', struct.pack('<I', 0)),
                   ('certificate-security-mode', struct.pack('<I', 1)),
                   ('effective-security-mode-sep', struct.pack('<I', 1))]
    if data_ramdisk or packed_data_ramdisk:
        chosen += [('ephemeral-storage', struct.pack('<I', 1))]
    if memory_size:
        chosen += [('darwinboot-memory-size', struct.pack('<Q', memory_size))]
    # Zero placeholders: DarwinBoot writes the address/size it actually owns, so
    # XNU only ever sees RAM this boot controls.
    chosen += [('dram-base', bytes(8)), ('dram-size', bytes(8))]
    chosen += [('crypto-hash-method', b'sha2-384\0')]
    if root_match_volume_uuid:
        chosen += [('root-matching', ('<dict><key>IOProviderClass</key><string>IOMedia</string>'
                                      '<key>IOPropertyMatch</key><dict><key>UUID</key><string>' +
                                      root_match_volume_uuid.upper() + '</string></dict></dict>\0').encode())]
    chosen += [('amfi-only-platform-code', struct.pack('<I', 1))]
    chosen += empty_nvram_proxy()

    memory_map = []
    if data_ramdisk or packed_data_ramdisk:
        memory_map.append(('PackedRAMDisk' if packed_data_ramdisk else 'RAMDisk', bytes(16)))
    if trust_cache:
        memory_map.append(('TrustCache', bytes(16)))

    return node('device-tree', [
        ('model', b'iPad7,1\0' if ipad_identity else b'oneplus,erhai\0'),
        *([('target-type', b'J120\0')] if ipad_identity else []),
        # Root compatible strings of this board's own compiled device tree.
        ('compatible', b'oneplus,erhai\0qcom,sm8750\0'),
        ('#address-cells', struct.pack('<I', 2)),
        ('#size-cells', struct.pack('<I', 2)),
    ], [
        node('chosen', chosen, [
            node('memory-map', memory_map),
            node('manifest-properties'),
        ]),
        node('cpus', children=cores),
        *([node('psci', [('compatible', b'arm,psci-1.0\0'), ('method', psci_method.encode() + b'\0')])]
          if len(cores) > 1 else []),
        node('defaults', [('sleep-disabled', struct.pack('<I', 1))]),
        node('options'),
        *([node('buttons', [('compatible', b'buttons\0'), ('device_type', b'buttons\0'),
                            # No 'menu' name: erhai has no Home button.
                            ('button-names', b'hold\0voldown\0volup\0'),
                            ('home-button-type', struct.pack('<I', 2))])] if ipad_identity else []),
        node('product', [('product-name', b'iPad Pro (12.9-inch) (2nd generation)\0' if ipad_identity
                          else b'OnePlus Pad 2 Pro\0'),
                         # OnePlus Pad 2 Pro is 12.1", but MobileGestalt and
                         # SpringBoard read geometry from artwork-*, and the
                         # target iPadOS image is qualified at 2732 @2x.
                         ('model', b'oneplus,erhai\0'),
                         *([('product-description', b'iPad Pro (12.9-inch) (2nd generation)\0'),
                            ('artwork-device-idiom', b'pad\0'),
                            ('artwork-device-subtype', struct.pack('<I', 2732)),
                            ('artwork-scale-factor', struct.pack('<I', 2)),
                            ('artwork-display-gamut', b'P3\0'),
                            ('compatible-device-fallback', b'iPad6,7\0'),
                            ('thin-bezel', struct.pack('<I', 1))] if ipad_identity else [])]),
        node('arm-io', [('device_type', b'arm-io\0'),
                        ('#address-cells', struct.pack('<I', 2)),
                        ('#size-cells', struct.pack('<I', 2)),
                        ('ranges', struct.pack('<QQQ', 0, ARMIO_BASE, ARMIO_SIZE))],
             [node('intc', [('compatible', b'arm,gic-v3\0'),
                            ('reg', struct.pack('<4Q',
                                                GIC_DIST - ARMIO_BASE, GIC_DIST_SIZE,
                                                GIC_REDIST - ARMIO_BASE, GIC_REDIST_SIZE)),
                            ('redistributor-stride', struct.pack('<Q', GIC_REDIST_STRIDE))]),
              # UFS sits at 0x1d84000, below the arm-io window, so the node is
              # only here for class matching; NativeUfs uses board constants.
              node('ufshc', [('compatible', b'qcom,sm8750-ufshc\0')])]),
    ])


def parse_cpu_regs(text):
    try:
        return [int(v, 0) for v in text.split(',')]
    except ValueError:
        raise argparse.ArgumentTypeError('--cpu-regs must be integers')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dram-size', type=lambda v: int(v, 0), required=True,
                        help='Physical DRAM size; DarwinBoot overwrites it with the owned window when zero')
    parser.add_argument('--owned-memory-size', type=lambda v: int(v, 0), default=None,
                        help='Contiguous window request, 256 MiB..8 GiB in 2 MiB steps')
    parser.add_argument('--root-match-volume-uuid', default=None)
    parser.add_argument('--unique-chip-id', type=lambda v: int(v, 16), default=None)
    parser.add_argument('--chip-id', type=lambda v: int(v, 16), default=None)
    parser.add_argument('--ipad-identity', action='store_true')
    parser.add_argument('--cpu-regs', default='0')
    parser.add_argument('--psci-method', choices=['smc', 'hvc'], default='smc')
    parser.add_argument('--packed-data-ramdisk', action='store_true')
    parser.add_argument('--data-ramdisk', action='store_true')
    parser.add_argument('--trust-cache', action='store_true')
    args = parser.parse_args(argv)

    # main() forwards; make_tree() owns every validity rule, so the CLI and the
    # contract test can never disagree about what is accepted.
    try:
        tree = make_tree(
            args.owned_memory_size if args.owned_memory_size is not None else OWNED_MIN,
            os.urandom(SEED_BYTES),
            dram_size=args.dram_size,
            unique_chip_id=args.unique_chip_id,
            chip_id=args.chip_id,
            ipad_identity=args.ipad_identity,
            cpu_regs=parse_cpu_regs(args.cpu_regs),
            psci_method=args.psci_method,
            packed_data_ramdisk=args.packed_data_ramdisk,
            data_ramdisk=args.data_ramdisk,
            trust_cache=args.trust_cache,
            root_match_volume_uuid=args.root_match_volume_uuid)
    except ValueError as error:
        parser.error(str(error))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(tree)
    owned = f'{args.owned_memory_size:#x}' if args.owned_memory_size is not None else 'default'
    print(f'erhai ADT: {len(tree)} bytes, {len(parse_cpu_regs(args.cpu_regs))} core(s), '
          f'GICv3 at {GIC_DIST:#x}, timebase {TIMEBASE}, dram {args.dram_size:#x}, '
          f'owned {owned}, ipad_identity {args.ipad_identity}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
