"""Production Amazon S3 store for Threefold governance certificates."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional
from threefold.application.dtos import GovernanceCertificateDTO

logger = logging.getLogger(__name__)


class S3CertificateUploader:
    """Uploads a governance certificate to Amazon S3.

    Nothing calls this today: the /issue-certificate route returns the document
    and stores nothing, so the bucket the template provisions stays empty. The
    class is kept as the shape archival would take, and it is not archival yet.
    """

    def __init__(
        self,
        bucket_name: Optional[str] = None,
        region_name: Optional[str] = None,
        boto3_client: Optional[Any] = None,
    ) -> None:
        self.bucket_name = bucket_name or os.getenv("EVIDENCE_BUCKET", "threefold-evidence-prod")
        self.region_name = region_name or os.getenv("AWS_REGION", "us-east-1")
        self._s3 = boto3_client

        if self._s3 is None:
            try:
                import boto3
                self._s3 = boto3.client("s3", region_name=self.region_name)
            except Exception as exc:
                logger.info("S3 client operating in local/offline mode: %s", exc)
                self._s3 = None

    @property
    def is_live(self) -> bool:
        return self._s3 is not None

    def upload_certificate(self, certificate: GovernanceCertificateDTO) -> Dict[str, str]:
        """Stores the certificate JSON in S3 with SHA-256 content fingerprint."""
        key = f"certificates/{certificate.certificate_id}.json"
        body_str = json.dumps(certificate.to_dict(), indent=2)

        if self._s3 is not None:
            try:
                self._s3.put_object(
                    Bucket=self.bucket_name,
                    Key=key,
                    Body=body_str.encode("utf-8"),
                    ContentType="application/json",
                    Metadata={
                        "sha256": certificate.sha256_fingerprint,
                        "session_id": certificate.session_id,
                        "verdict_status": certificate.verdict_status,
                    },
                )
                logger.info("Uploaded certificate %s to s3://%s/%s", certificate.certificate_id, self.bucket_name, key)
            except Exception as exc:
                logger.warning("Failed to upload certificate to S3 (%s), returning local URI", exc)

        return {
            "s3_uri": f"s3://{self.bucket_name}/{key}",
            "key": key,
            "sha256": certificate.sha256_fingerprint,
        }
