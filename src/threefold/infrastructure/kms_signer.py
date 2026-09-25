"""Signs governance certificates with the stack's KMS key, when it has one.

The key id arrives as CERT_KMS_KEY_ID. Local runs, tests and stacks deployed
before the key existed set nothing, and signing is then honestly absent: the
certificate carries its fingerprint and no signature, and says so. Where the
key is configured, a signing failure raises rather than degrading to an
unsigned certificate, because a quiet downgrade would be the lie the
signature exists to prevent.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger("threefold.kms")


def sign_certificate(canonical: bytes) -> Optional[Tuple[str, str]]:
    """The (base64 signature, key id) over the certificate's canonical bytes.

    None when no key is configured. Raises when one is configured and KMS
    refuses, so the caller fails loudly instead of issuing unsigned.
    """
    key_id = os.environ.get("CERT_KMS_KEY_ID", "")
    if not key_id:
        return None
    import boto3  # lazy, like every AWS client: tests never import it

    kms = boto3.client("kms")
    response = kms.sign(
        KeyId=key_id,
        Message=canonical,
        MessageType="RAW",
        SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256",
    )
    return base64.b64encode(response["Signature"]).decode("ascii"), key_id
