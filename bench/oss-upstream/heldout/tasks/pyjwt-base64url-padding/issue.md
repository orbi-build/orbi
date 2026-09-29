# Canonical Base64URL check rejects AWS ALB JWTs

## Problem

The current release rejects the OIDC JWTs that AWS Application Load Balancer puts in the
`x-amzn-oidc-data` header: `jwt.decode()` raises `DecodeError: Invalid crypto padding`
before the signature is even checked. The same tokens decoded fine on the previous minor
release.

The reason is that ALB emits JWS segments with standard trailing `=` padding (a known
non-compact variant), and the strict Base64URL segment check added recently for a security
advisory rejects any segment that is not byte-for-byte the canonical unpadded encoding.
That advisory only needed to stop *junk* from being silently ignored (for example a
signature with `!!!!` appended still verifying, because Python's `base64.urlsafe_b64decode`
is lenient and drops invalid characters).

## Reproduction

```python
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

key = ec.generate_private_key(ec.SECP256R1())
priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                         serialization.NoEncryption())
pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                    serialization.PublicFormat.SubjectPublicKeyInfo)
token = jwt.encode({"email": "a@b.com", "exp": 9999999999}, priv, algorithm="ES256")
h, p, s = token.split(".")

padded = f"{h}.{p}.{s}{'=' * ((4 - len(s) % 4) % 4)}"

jwt.decode(token, pub, algorithms=["ES256"])    # ok
jwt.decode(padded, pub, algorithms=["ES256"])   # DecodeError: Invalid crypto padding
```

The same happens for HS256 tokens whose signature segment is given its `=` padding.

## Expected behavior

- A segment that is the canonical Base64URL encoding **plus the correct trailing `=`
  padding** is accepted and decodes to the same bytes as the unpadded form. E.g. at the
  segment level: `Zg` -> `b"f"`, `Zg==` -> `b"f"`, `Zm8=` -> `b"fo"`. A padded ALB-style
  ES256 or HS256 token verifies just like its compact form.
- Everything else stays rejected with `DecodeError` ("Invalid crypto padding" for the
  signature segment), in particular:
  - characters outside the Base64URL alphabet (`Z?==`, appended `!!!!` junk);
  - the wrong amount of padding (`Zg=`, `Zg===`) or `=` anywhere other than the end
    (e.g. inserted in the middle of a signature);
  - impossible lengths (`a`);
  - non-canonical encodings whose unused trailing bits are not zero (`Zh`, `Zh==`) --
    keep rejecting these as today.
- Do not "fix" this by decoding and re-encoding the token before verification: the lenient
  standard-library decoder would re-open the junk-acceptance hole described above.

## Acceptance

- Fix the problem described above.
- Add tests covering these scenarios (accepted padded forms, and each rejected form).
- The repository's own test suite passes: `pytest tests`
