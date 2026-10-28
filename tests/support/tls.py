"""Ephemeral loopback TLS certificate generator for Voice AI test infrastructure.

Generates self-signed TLS certificates for 127.0.0.1 and localhost with restrictive
(0o600) file permissions, avoiding any system keychain or system trust modifications.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def generate_ephemeral_loopback_cert(
    cert_path: Path,
    key_path: Path,
    *,
    validity_days: int = 1,
) -> tuple[Path, Path]:
    """Generate ephemeral self-signed certificate for 127.0.0.1 and localhost.

    Files are created with restrictive 0o600 permissions.
    """
    cert_path = Path(cert_path).resolve()
    key_path = Path(key_path).resolve()
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Generate 2048-bit RSA private key
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # 2. Build X.509 certificate with Subject Alternative Names
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Voice AI Ephemeral Loopback Ingress"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=max(1, validity_days)))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                x509.DNSName("localhost"),
            ]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    # 3. Serialize PEM bytes
    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)

    # 4. Write with restrictive (0o600) permissions
    for p, data in ((key_path, key_bytes), (cert_path, cert_bytes)):
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(p, flags, 0o600)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        with open(fd, "wb") as f:
            f.write(data)

    return cert_path, key_path
