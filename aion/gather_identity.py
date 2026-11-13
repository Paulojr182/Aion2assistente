"""Stable collection identity shared by observation and delivery guards."""


def format_gather_identity(target):
    identity=target['identity'];source=target['source'];key=target['environment_key']
    if (not isinstance(identity,(tuple,list)) or len(identity)!=2
            or any(type(value) is not int for value in identity)
            or identity[0]<0 or not 0<identity[1]<=0xffffffff
            or type(source.get('id')) is not int or not 0<source['id']<=0xffffffff):
        raise ValueError('Identidade nativa de recurso inválida.')
    if type(key) not in (int,str) or not str(key).isdigit() or not 0<int(key)<2**64:
        raise ValueError('Chave de ambiente inválida.')
    return f"env:{int(key)}:{identity[0]}:{identity[1]}:{source['id']}"
