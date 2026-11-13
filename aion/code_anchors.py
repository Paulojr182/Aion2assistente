"""Bounded relocation-aware code checks shared by read-only observers."""
from .metadata_templates import InstructionTemplate, MetadataRecoveryError


def recover_code_anchors(read, image_size, templates, *, max_bytes=33554432, page_size=4096):
    templates = tuple(templates)
    if (not templates or len({t.name for t in templates}) != len(templates)
            or type(image_size) is not int or image_size <= 0
            or type(max_bytes) is not int or not 0 < max_bytes <= 67108864
            or type(page_size) is not int or not 256 <= page_size <= 1048576):
        raise MetadataRecoveryError('Limites ou modelos de código inválidos.')
    used = 0

    def capture(rva, size):
        nonlocal used
        used += size
        if used > max_bytes:
            raise MetadataRecoveryError('Orçamento de código esgotado.')
        raw = read(rva, size)
        if type(raw) is not bytes or len(raw) != size:
            raise MetadataRecoveryError('Leitura de código incompleta.')
        return raw

    anchors = []
    for template in templates:
        if (type(template.radius) is not int or not 0 <= template.radius <= 4194304
                or not 0 <= template.old_rva < image_size
                or not 0 < len(template.expected) <= image_size-template.old_rva):
            raise MetadataRecoveryError('Modelo fora dos limites da imagem.')
        pattern = template.pattern()
        matches = {}
        low = max(0, template.old_rva-template.radius)
        # A readable instruction page can border an unavailable page. Starting
        # at an unaligned old RVA would make each read straddle both pages and
        # hide valid code behind ERROR_PARTIAL_COPY. Patterns remain unchanged.
        alignment=min(page_size,4096)
        low=low//alignment*alignment
        high = min(image_size, template.old_rva+template.radius+len(template.expected))
        carry = b''
        for rva in range(low, high, page_size):
            size = min(page_size, high-rva)
            try:raw = capture(rva, size)
            except OSError:
                carry = b''
                continue
            joined = carry+raw
            origin = rva-len(carry)
            for match in pattern.finditer(joined):
                candidate = origin+match.start()
                if abs(candidate-template.old_rva) <= template.radius:
                    matches[candidate] = match.group()
            carry = joined[-(len(template.expected)-1):] if len(template.expected) > 1 else b''
        if len(matches) != 1:
            raise MetadataRecoveryError(f'{template.name}: {len(matches)} candidatos de código.')
        rva, raw = next(iter(matches.items()))
        template.targets(raw, rva, image_size)
        if capture(rva, len(raw)) != raw:
            raise MetadataRecoveryError('Código mudou durante a recuperação.')
        anchors.append((template.name, rva, raw))
    return tuple(anchors)


def verify_code_anchors(read, anchors):
    for name, rva, expected in anchors:
        if read(rva, len(expected)) != expected:
            raise MetadataRecoveryError('Código mudou: '+name)


def validate_captured_anchors(process, records, definitions, required):
    """Bind saved evidence to original instruction patterns and this process."""
    if not records or records[-1].get('session') != process.session:
        raise MetadataRecoveryError('Evidência de código pertence a outra sessão.')
    anchors = []
    for name in required:
        matches = [r for r in records if r.get('name') == name]
        sources = [d for d in definitions if d.get('name') == name]
        if (len(matches) != 1 or len(sources) != 1
                or type(matches[0].get('matches')) is not int or matches[0]['matches'] != 1):
            raise MetadataRecoveryError('Assinatura ausente ou duplicada: '+name)
        record, source = matches[0], sources[0]
        raw = bytes.fromhex(record['current'])
        rva = record['rva']
        if (type(rva) is not int or not raw or not 0 <= rva <= process.image_size-len(raw)):
            raise MetadataRecoveryError('Assinatura fora da imagem: '+name)
        template = InstructionTemplate(name, source['old_rva'], bytes.fromhex(source['expected']),
                                       tuple(tuple(p) for p in source['operands']))
        template.targets(raw, rva, process.image_size)
        anchors.append((name, rva, raw))
    verify_code_anchors(lambda rva,size:process.read(process.base+rva,size), anchors)
    return tuple(anchors)


def recover_named_anchors(process, definitions, required):
    required = tuple(required)
    selected = [d for d in definitions if d['name'] in required]
    if (not required or len(set(required)) != len(required) or len(selected) != len(required)
            or {d['name'] for d in selected} != set(required)):
        raise MetadataRecoveryError('Modelos de código incompletos ou duplicados.')
    templates = tuple(InstructionTemplate(d['name'],d['old_rva'],bytes.fromhex(d['expected']),
        tuple(tuple(p) for p in d['operands']),d['radius']) for d in selected)
    return recover_code_anchors(lambda rva,size:process.read(process.base+rva,size),process.image_size,templates)
