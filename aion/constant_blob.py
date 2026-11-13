"""Bounded static reader for Nuitka Python 3.11 constant records.

Format reference (not executable code loaded from the inspected application):
https://github.com/Nuitka/Nuitka/blob/develop/nuitka/build/static_src/HelpersConstantsBlob.c
Produces data descriptions only; never instantiates code objects or imports
names from the blob. Unsupported encodings fail with the exact stream offset.
"""
import struct


class BlobError(ValueError):
    pass


class ConstantBlob:
    def __init__(self, data, *, max_items=100000, max_depth=64):
        self.data = data
        self.offset = 0
        self.remaining = max_items
        self.max_depth = max_depth

    def read(self, size):
        if not 0 <= size <= len(self.data) - self.offset:
            raise BlobError(f'Truncated blob at {self.offset}: {size} bytes')
        result = self.data[self.offset:self.offset + size]
        self.offset += size
        return result

    def uint(self):
        result = 0
        for shift in range(0, 70, 7):
            value = self.read(1)[0]
            result |= (value & 127) << shift
            if value < 128:
                return result
        raise BlobError('Variable integer too long')

    def string(self):
        end = self.data.find(b'\0', self.offset)
        if end < 0:
            raise BlobError('Unterminated string')
        raw = self.read(end - self.offset + 1)[:-1]
        return raw.decode('utf-8', 'surrogatepass')

    def sequence(self, count, depth):
        if count > self.remaining:
            raise BlobError('Item budget exceeded')
        items = []
        for _ in range(count):
            items.append(self.value(items, depth))
        return items

    def value(self, previous, depth=0):
        if depth > self.max_depth or self.remaining <= 0:
            raise BlobError('Depth/item budget exceeded')
        self.remaining -= 1
        position = self.offset
        tag = chr(self.read(1)[0])
        if tag == 'p':
            if not previous:
                raise BlobError('Previous value missing')
            return previous[-1]
        if tag in 'nstF':
            return {'n': None, 's': '', 't': True, 'F': False}.get(tag)
        if tag in 'lq':
            number = self.uint()
            return number if tag == 'l' else -number
        if tag in 'gG':
            number = 0
            count = self.uint()
            if count > 1024:
                raise BlobError('Oversized integer')
            for _ in range(count):
                number = (number << 31) + self.uint()
            return number if tag == 'g' else -number
        if tag == 'f':
            return struct.unpack('<d', self.read(8))[0]
        if tag == 'j':
            return {'complex': list(struct.unpack('<dd', self.read(16)))}
        if tag in 'auOE':
            text = self.string()
            return text if tag in 'au' else {'builtin_type': tag, 'name': text}
        if tag == 'w':
            return self.read(1).decode('utf-8', 'surrogatepass')
        if tag == 'v':
            return self.read(self.uint()).decode('utf-8', 'surrogatepass')
        if tag in 'bBdX':
            size = 1 if tag == 'd' else self.uint()
            return {'bytes_type': tag, 'hex': self.read(size).hex()}
        if tag == 'c':
            # NUITKA_CONSTANT_BLOB_TAG_BYTES_ZERO_TERMINATED; raw bytes,
            # independent of Unicode decoding (constants_blob_spec.h).
            end = self.data.find(b'\0', self.offset)
            if end < 0:
                raise BlobError('Unterminated byte string')
            return {'bytes_type': tag, 'hex': self.read(end-self.offset+1)[:-1].hex()}
        if tag in 'MQZ':
            return {'special_type': tag, 'index': self.read(1)[0]}
        if tag in 'TLPS':
            return {'container': tag, 'items': self.sequence(self.uint(), depth + 1)}
        if tag == 'D':
            size = self.uint()
            return {'container': tag, 'keys': self.sequence(size, depth + 1),
                    'values': self.sequence(size, depth + 1)}
        if tag in ':;AJH':
            count = {':': 3, ';': 3, 'A': 2, 'J': 2, 'H': 1}[tag]
            return {'container': tag, 'items': self.sequence(count, depth + 1)}
        if tag in 'CKWY':
            flags = self.uint()
            if tag == 'K':
                name, line, arguments, count = '<module>', 1, {'container': 'T', 'items': []}, 0
            else:
                name = '<genexpr>' if tag == 'Y' else self.value([], depth + 1)
                line = self.uint() + 1
                arguments = self.value([], depth + 1)
                count = 0 if tag == 'W' else 1 if tag == 'Y' else self.uint()
            qualified = self.value([], depth + 1) if flags & 1 else name
            free = self.value([], depth + 1) if flags & 2 else None
            kw = self.uint() + 1 if flags & 4 else 0
            pos = self.uint() + 1 if flags & 8 else 0
            return {'code_metadata': True, 'flags': flags, 'name': name,
                    'line': line, 'arguments': arguments, 'arg_count': count,
                    'qualname': qualified, 'free_vars': free, 'kw_only': kw, 'pos_only': pos}
        raise BlobError(f'Unsupported tag {tag!r} at {position}')

    def decode(self):
        count = int.from_bytes(self.read(2), 'little')
        items = self.sequence(count, 0)
        # Module record sizes include a terminating '.' sentinel.
        trailing = self.data[self.offset:]
        if trailing not in (b'', b'.'):
            raise BlobError(f'Unexpected trailing bytes at {self.offset}: {trailing[:16].hex()}')
        return items
