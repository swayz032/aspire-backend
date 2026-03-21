"""KMS PayloadCodec — Enhancement #6: Encrypt Temporal history at rest.

Temporal's RDS Postgres stores activity inputs/outputs as plaintext JSON.
This codec encrypts all payloads using AWS KMS envelope encryption:
  - AES-256-GCM data key for payload encryption
  - KMS CMK wraps the data key
  - Fail closed if KMS unavailable (Law #3)

Cost: ~$1/mo KMS key + $0.03/10K encrypt/decrypt requests.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec

logger = logging.getLogger(__name__)


class KmsPayloadCodec(PayloadCodec):
    """AWS KMS envelope encryption codec for Temporal payloads.

    Encrypts payloads with AES-256-GCM using a per-payload data key.
    The data key is encrypted (wrapped) with the KMS CMK.
    """

    ENCODING = b"binary/encrypted"
    METADATA_KEY_ENCRYPTED_KEY = "encryption-key"
    METADATA_KEY_NONCE = "encryption-nonce"

    def __init__(self, kms_key_arn: str) -> None:
        self._kms_key_arn = kms_key_arn
        self._kms_client: Any = None

        # Fail fast: verify dependencies at init, not first use (Law #3)
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
            import boto3  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                f"KMS PayloadCodec requires 'cryptography' and 'boto3' packages: {e}"
            ) from e

    def _get_kms_client(self) -> Any:
        if self._kms_client is None:
            import boto3

            self._kms_client = boto3.client(
                "kms",
                region_name=os.getenv("AWS_REGION", "us-east-1"),
            )
        return self._kms_client

    async def encode(self, payloads: list[Payload]) -> list[Payload]:
        """Encrypt each payload with a fresh AES-256-GCM data key."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        kms = self._get_kms_client()
        result: list[Payload] = []

        for payload in payloads:
            # Generate data key via KMS
            key_response = kms.generate_data_key(
                KeyId=self._kms_key_arn,
                KeySpec="AES_256",
            )
            plaintext_key = key_response["Plaintext"]
            encrypted_key = key_response["CiphertextBlob"]

            # Encrypt payload data with AES-256-GCM
            nonce = os.urandom(12)
            aesgcm = AESGCM(plaintext_key)
            ciphertext = aesgcm.encrypt(nonce, payload.SerializeToString(), None)

            # Build encrypted payload with metadata
            result.append(
                Payload(
                    metadata={
                        "encoding": self.ENCODING,
                        self.METADATA_KEY_ENCRYPTED_KEY: base64.b64encode(encrypted_key),
                        self.METADATA_KEY_NONCE: base64.b64encode(nonce),
                    },
                    data=ciphertext,
                )
            )

        return result

    async def decode(self, payloads: list[Payload]) -> list[Payload]:
        """Decrypt each payload using KMS-unwrapped data key."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        kms = self._get_kms_client()
        result: list[Payload] = []

        for payload in payloads:
            # Skip unencrypted payloads
            if payload.metadata.get("encoding") != self.ENCODING:
                result.append(payload)
                continue

            # Decrypt data key via KMS
            encrypted_key = base64.b64decode(
                payload.metadata[self.METADATA_KEY_ENCRYPTED_KEY]
            )
            nonce = base64.b64decode(payload.metadata[self.METADATA_KEY_NONCE])

            try:
                key_response = kms.decrypt(CiphertextBlob=encrypted_key)
            except Exception:
                # Law #3: Fail closed — KMS unavailable = deny
                logger.error("KMS decrypt failed — fail closed (Law #3)")
                raise RuntimeError("KMS unavailable — cannot decrypt Temporal payload (Law #3 fail-closed)")

            plaintext_key = key_response["Plaintext"]

            # Decrypt payload
            aesgcm = AESGCM(plaintext_key)
            plaintext = aesgcm.decrypt(nonce, payload.data, None)

            # Reconstruct original payload
            original = Payload()
            original.ParseFromString(plaintext)
            result.append(original)

        return result


class NoOpPayloadCodec(PayloadCodec):
    """Pass-through codec for local dev (no encryption)."""

    async def encode(self, payloads: list[Payload]) -> list[Payload]:
        return payloads

    async def decode(self, payloads: list[Payload]) -> list[Payload]:
        return payloads


def create_payload_codec() -> PayloadCodec:
    """Factory: KMS codec in production, no-op in dev."""
    kms_key_arn = os.getenv("TEMPORAL_KMS_KEY_ARN")
    if kms_key_arn:
        logger.info("Temporal PayloadCodec: KMS encryption enabled (key=***%s)", kms_key_arn[-4:])
        return KmsPayloadCodec(kms_key_arn)
    logger.info("Temporal PayloadCodec: no-op (dev mode, TEMPORAL_KMS_KEY_ARN not set)")
    return NoOpPayloadCodec()
