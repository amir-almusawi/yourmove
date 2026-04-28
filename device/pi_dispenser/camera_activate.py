"""Activate an unactivated Hikvision/Annke camera via ISAPI challenge-response."""
from __future__ import annotations

import base64
import logging
import xml.etree.ElementTree as ET

import requests
from cryptography.hazmat.primitives.asymmetric import rsa, padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger(__name__)

ISAPI_CHALLENGE = "/ISAPI/Security/challenge"
ISAPI_ACTIVATE = "/ISAPI/System/activate"
TIMEOUT = 10


def _rsa_modulus_hex(pub_key) -> str:
    """Get the RSA modulus as a hex string (cryptico publicKeyString format)."""
    pub_numbers = pub_key.public_numbers()
    n_hex = format(pub_numbers.n, 'x')
    return n_hex


def _aes_encrypt_ecb(plaintext: bytes, key: bytes) -> bytes:
    """AES-128 ECB with zero-padding (matching Hikvision's custom JS AES)."""
    key = key[:16]
    if len(key) < 16:
        key += b'\x00' * (16 - len(key))
    if len(plaintext) < 16:
        plaintext += b'\x00' * (16 - len(plaintext))
    elif len(plaintext) > 16:
        plaintext = plaintext[:16]
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    enc = cipher.encryptor()
    return enc.update(plaintext) + enc.finalize()


def activate_camera(ip: str, password: str) -> bool:
    """Activate an unactivated Hikvision/ISAPI camera using the challenge-response protocol.

    Returns True on success.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    pub_key = private_key.public_key()
    modulus_hex = _rsa_modulus_hex(pub_key)
    pub_key_b64 = base64.b64encode(modulus_hex.encode()).decode()

    challenge_xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<PublicKey><key>{pub_key_b64}</key></PublicKey>"
    )

    try:
        r = requests.post(
            f"http://{ip}{ISAPI_CHALLENGE}",
            data=challenge_xml,
            headers={"Content-Type": "application/xml"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            log.error("Challenge request failed: %d %s", r.status_code, r.text[:200])
            return False
    except Exception as e:
        log.error("Challenge request error: %s", e)
        return False

    try:
        root = ET.fromstring(r.text)
        key_el = None
        for el in root.iter():
            if el.tag.split("}")[-1] == "key" and el.text:
                key_el = el
                break
        if key_el is None:
            log.error("No key in challenge response: %s", r.text[:200])
            return False
        encrypted_b64 = key_el.text.strip()
    except ET.ParseError as e:
        log.error("Failed to parse challenge response: %s", e)
        return False

    try:
        decoded = base64.b64decode(encrypted_b64)
        decoded_text = decoded.decode("ascii")
        encrypted_bytes = bytes.fromhex(decoded_text)
        random_bytes = private_key.decrypt(encrypted_bytes, asym_padding.PKCS1v15())
        random_str = random_bytes.decode("ascii", errors="replace")
        log.info("Challenge decrypted, random string: %d chars", len(random_str))
    except Exception as e:
        log.error("RSA decryption failed: %s", e)
        return False

    aes_key = bytes.fromhex(random_str)

    prefix_encrypted = _aes_encrypt_ecb(random_str[:16].encode(), aes_key).hex()
    password_encrypted = _aes_encrypt_ecb(password.encode(), aes_key).hex()
    combined = prefix_encrypted + password_encrypted
    payload_b64 = base64.b64encode(combined.encode()).decode()

    activate_xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<ActivateInfo><password>{payload_b64}</password></ActivateInfo>"
    )

    try:
        r = requests.put(
            f"http://{ip}{ISAPI_ACTIVATE}",
            data=activate_xml,
            headers={"Content-Type": "application/xml"},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            log.info("Camera at %s activated successfully", ip)
            return True
        log.error("Activation failed: %d %s", r.status_code, r.text[:300])
        return False
    except Exception as e:
        log.error("Activation request error: %s", e)
        return False
