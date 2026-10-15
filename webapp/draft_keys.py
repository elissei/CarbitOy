"""Server-only HMAC keyring. No defaults, credential generation or HTTP API."""

import base64
import hashlib
import hmac
import json
import re


class DraftKeyConfigurationError(ValueError):
    def __init__(self):
        super().__init__('draft fingerprint key configuration unavailable')


class DraftKeyring:
    def __init__(self, *, active_key_id, keys, legacy_key_id=None):
        valid_id = lambda value: type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value)
        if (type(keys) is not dict or not keys or len(keys)>32
                or not valid_id(active_key_id) or active_key_id not in keys
                or any(not valid_id(k) or type(v) is not bytes or len(v)<32
                       or len(set(v))<16 for k,v in keys.items())
                or (legacy_key_id is not None and legacy_key_id not in keys)):
            raise DraftKeyConfigurationError()
        # Entropy cannot be proven from bytes. Operators must obtain keys from a
        # CSPRNG; the diversity check only rejects obviously weak configuration.
        self.active_key_id = active_key_id
        self.legacy_key_id = legacy_key_id
        self._keys = dict(keys)

    @classmethod
    def from_environment(cls, environment):
        """Explicit supplied mapping; no env mutations or secret-bearing errors."""
        def unique_members(pairs):
            result = {}
            for key,value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        try:
            source = environment['CARBIT_DRAFT_HMAC_KEYS']
            if type(source) is not str or len(source)>32768:
                raise ValueError()
            encoded = json.loads(source,object_pairs_hook=unique_members)
            if type(encoded) is not dict:
                raise ValueError()
            keys = {k:base64.b64decode(v,validate=True) for k,v in encoded.items()}
            return cls(active_key_id=environment['CARBIT_DRAFT_HMAC_ACTIVE_KEY_ID'],
                       keys=keys,legacy_key_id=environment.get('CARBIT_DRAFT_HMAC_LEGACY_KEY_ID'))
        except (KeyError,ValueError,TypeError,RecursionError):
            raise DraftKeyConfigurationError() from None

    def fingerprint(self, message, *, key_id):
        resolved = self.legacy_key_id if key_id is None else key_id
        if resolved not in self._keys:
            raise DraftKeyConfigurationError()
        return hmac.new(self._keys[resolved],message,hashlib.sha256).hexdigest()

    def require(self, key_id):
        resolved = self.legacy_key_id if key_id is None else key_id
        if resolved not in self._keys:
            raise DraftKeyConfigurationError()

    def verify(self, message, *, key_id, fingerprint):
        return hmac.compare_digest(self.fingerprint(message,key_id=key_id),fingerprint)
