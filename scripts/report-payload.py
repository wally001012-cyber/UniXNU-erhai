#!/usr/bin/env python3
"""Say what the built payload actually contains, so a run does not have to be
downloaded and unzipped to find out.

The one question that matters after a build is: did the Apple kexts make it in?
A source-only kernel has __PRELINK_TEXT size 0 and cannot get past the platform
expert, while a prelinked one carries __PRELINK_TEXT / __PRELINK_INFO with real
extents. This prints that, plus the ADT identity, straight into the run log.

Written to out/<board>/report/payload-report.txt and echoed.
"""
import json
import struct
import sys
from pathlib import Path

SEG_NAMES = ('__TEXT', '__PRELINK_TEXT', '__PRELINK_INFO', '__PLK_TEXT_EXEC',
             '__TEXT_EXEC', '__DATA', '__KLDDATA')


def segments(raw):
    if struct.unpack_from('<I', raw, 0)[0] != 0xFEEDFACF:
        return []
    ncmds = struct.unpack_from('<I', raw, 16)[0]
    off, out = 32, []
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', raw, off)
        if cmd == 0x19:
            name = raw[off + 8:off + 24].rstrip(b'\0').decode('ascii', 'replace')
            va, vsz = struct.unpack_from('<QQ', raw, off + 24)
            out.append((name, va, vsz))
        off += size
    return out


def prelink_info(raw):
    """Return the __PRELINK_INFO blob.

    It is a *binary* plist (bplist00), not XML: looking for '<?xml' finds an
    unrelated fragment further down the file and makes both the kext count and
    the pointer-table check wrong. The blob starts at the bplist magic right
    after the kext text and ends at the end of that bplist's trailer.
    """
    start = raw.find(b'bplist00')
    if start < 0:
        return b''
    # A bplist is: header(8) + objects + offset table + 32-byte trailer.
    # The trailer's last 8 bytes are the offset-table offset; the blob length is
    # not stored, so take the mapped window from the start of the bplist to the
    # next Mach-O segment (or the end of the file).
    tail = raw.find(b'bplist00', start + 8)
    end = tail if tail > 0 else len(raw)
    return raw[start:end]


def read_fat(img):
    d = img.read_bytes()
    bps = struct.unpack_from('<H', d, 11)[0]
    spc = d[13]
    rsvd = struct.unpack_from('<H', d, 14)[0]
    nfat = d[16]
    fatsz32 = struct.unpack_from('<I', d, 36)[0]
    data_start = rsvd + nfat * fatsz32
    cls = bps * spc
    fat = d[rsvd * bps:rsvd * bps + fatsz32 * bps]

    def chain(start, cap=1 << 26):
        buf, c = bytearray(), start
        while 2 <= c < 0x0FFFFFF8 and len(buf) < cap:
            off = (data_start + (c - 2) * spc) * bps
            buf += d[off:off + cls]
            c = struct.unpack_from('<I', fat, c * 4)[0] & 0x0FFFFFFF
        return bytes(buf)

    def walk(start, prefix, depth=0):
        if depth > 4:
            return []
        out = []
        blob = chain(start)
        for i in range(0, len(blob), 32):
            e = blob[i:i + 32]
            if len(e) < 32 or e[0] == 0:
                break
            if e[0] == 0xE5 or e[11] == 0x0F:
                continue
            name = e[0:8].decode('latin1').rstrip()
            ext = e[8:11].decode('latin1').rstrip()
            if name in ('.', '..'):
                continue
            clus = struct.unpack_from('<H', e, 20)[0] << 16 | struct.unpack_from('<H', e, 26)[0]
            size = struct.unpack_from('<I', e, 28)[0]
            full = f'{prefix}/{name}' + (f'.{ext}' if ext else '')
            if e[11] & 0x10:
                out += walk(clus, full, depth + 1)
            else:
                out.append((full, size, clus))
        return out

    return walk(struct.unpack_from('<I', d, 44)[0], ''), chain


def adt_props(data):
    """Return {name: bytes} for every property, flattening the node tree."""
    out = {}

    def walk(off, end):
        while off + 8 <= end:
            props, children = struct.unpack_from('<II', data, off)
            off += 8
            if props > 0x10000 or children > 0x10000 or off > end:
                return off
            for _ in range(props):
                if off + 36 > end:
                    return end
                name = data[off:off + 32].rstrip(b'\0').decode('ascii', 'replace')
                ln = struct.unpack_from('<I', data, off + 32)[0]
                out.setdefault(name, data[off + 36:off + 36 + ln])
                off += 36 + ((ln + 3) & ~3)
            for _ in range(children):
                off = walk(off, end)
        return off

    walk(0, len(data))
    return out


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else 'out/erhai')
    lines = []

    def say(text=''):
        print(text)
        lines.append(text)

    kernel = root / 'kernel/kernel.macho'
    payload = root / 'payload/payload.img'
    report = root / 'payload/payload.json'

    if kernel.is_file():
        raw = kernel.read_bytes()
        segs = segments(raw)
        say(f'kernel.macho : {len(raw):,} B')
        pre = next((s for s in segs if s[0] == '__PRELINK_TEXT'), None)
        info = next((s for s in segs if s[0] == '__PRELINK_INFO'), None)
        for name, va, sz in segs:
            if name in SEG_NAMES:
                say(f'  {name:<16} va 0x{va:016x} size 0x{sz:x}')
        if pre and pre[2]:
            say(f'  -> PRELINKED: __PRELINK_TEXT carries 0x{pre[2]:x} bytes of Apple kexts')
            # __PRELINK_INFO is a binary plist listing every injected executable;
            # counting CFBundleIdentifier keys is the honest "how many kexts".
            blob = prelink_info(raw)
            count = blob.count(b'CFBundleIdentifier')
            if count:
                say(f'  -> __PRELINK_INFO lists {count} kext(s), 0x{len(blob):x} B of bplist')
            else:
                say('  -> __PRELINK_INFO carries no CFBundleIdentifier; nothing was injected')
            # The pointer table XNU walks to rebase them lives in __PRELINK_TEXT.
            if b'_PrelinkLinkKASLROffsets' not in raw:
                say('  -> WARNING: no _PrelinkLinkKASLROffsets; XNU cannot rebase them')
            else:
                say('  -> pointer table _PrelinkLinkKASLROffsets present')
        elif pre:
            say('  -> SOURCE ONLY: __PRELINK_TEXT is empty, no Apple drivers, '
                'the platform expert cannot be reached past')
        else:
            say('  -> no __PRELINK_TEXT segment at all')
    else:
        say(f'kernel.macho : absent ({kernel})')

    if payload.is_file():
        say(f'\npayload.img  : {payload.stat().st_size:,} B')
        files, chain = read_fat(payload)
        for full, size, clus in files:
            say(f'  {full:<44} {size:>12,} B')
        adt = next((f for f in files if f[0].upper().endswith('.ADT')), None)
        if adt:
            props = adt_props(chain(adt[2])[:adt[1]])
            say('\n  ADT identity:')
            for key in ('model', 'target-type', 'chip-id', 'unique-chip-id',
                        'product-name', 'compatible-device-fallback'):
                if key not in props:
                    say(f'    {key:<26} MISSING')
                    continue
                val = props[key]
                text = val.rstrip(b'\0').decode('utf-8', 'replace')
                if text and all(32 <= ord(c) < 127 for c in text):
                    say(f'    {key:<26} {text!r}')
                else:
                    say(f'    {key:<26} 0x{int.from_bytes(val, "little"):x}')
            if 'target-type' not in props:
                say('    -> NOT an iPad identity; iOS userland will refuse this tree')
    else:
        say(f'payload.img  : absent ({payload})')

    if report.is_file():
        data = json.loads(report.read_text())
        say('\npayload.json:')
        for key in ('kernel_sha256', 'payload_kernel_sha256', 'adt_sha256',
                    'kernel_args', 'panic_screen', 'payload_bytes'):
            say(f'  {key:<22} {data.get(key)}')

    out = root / 'report'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'payload-report.txt').write_text('\n'.join(lines) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
