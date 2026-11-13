"""Separate, atomic local preferences. Never reads the original bot's secrets."""
from pathlib import Path
import json, os, tempfile


DEFAULT = {'schema': 1, 'macro_key': 'ç', 'language': 'Português (Brasil)',
           'skills': [], 'target_names': [], 'options': {}}


class Profiles:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'profile.json'

    def load(self):
        if not self.path.exists():
            return json.loads(json.dumps(DEFAULT))
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('schema') != 1:
            raise ValueError('Formato de perfil desconhecido. O arquivo foi preservado.')
        return {**json.loads(json.dumps(DEFAULT)), **data}

    def save(self, data):
        if data.get('schema') != 1:
            raise ValueError('Versão de perfil inválida.')
        payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='profile-', suffix='.tmp', dir=self.root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
