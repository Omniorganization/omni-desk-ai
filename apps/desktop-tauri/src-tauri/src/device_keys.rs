//! Desktop device signing key, kept out of the webview.
//!
//! The P-256 private key is generated and used only in Rust. The webview gets
//! the device id, the public key and request signatures, so script running in
//! the webview (a future XSS or a malicious bundled dependency) can no longer
//! read or exfiltrate the long-term key. Keys created by earlier releases with
//! WebCrypto are stored in the same JWK format and keep working.

use base64::{
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
    Engine,
};
use p256::ecdsa::{signature::Signer, Signature, SigningKey};
use p256::pkcs8::{EncodePublicKey, LineEnding};
use p256::SecretKey;
use rand_core::{OsRng, RngCore};

pub struct GeneratedIdentity {
    pub device_id: String,
    pub public_key_pem: String,
    pub private_jwk: String,
}

pub fn generate_identity() -> Result<GeneratedIdentity, String> {
    let secret = SecretKey::random(&mut OsRng);
    let public_key_pem = secret
        .public_key()
        .to_public_key_pem(LineEnding::LF)
        .map_err(|_| "could not encode device public key".to_string())?
        .trim_end()
        .to_string();
    let mut id_bytes = [0u8; 18];
    OsRng.fill_bytes(&mut id_bytes);
    let device_id = format!(
        "desk_{}",
        id_bytes
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
    );
    Ok(GeneratedIdentity {
        device_id,
        public_key_pem,
        private_jwk: secret.to_jwk_string().to_string(),
    })
}

fn is_hex(value: &str, len: usize) -> bool {
    value.len() == len && value.bytes().all(|b| b.is_ascii_hexdigit())
}

/// Builds the gateway's device request message from validated parts, so the
/// key cannot be used to sign arbitrary attacker-chosen payloads.
pub fn request_message(
    method: &str,
    path: &str,
    body_sha256: &str,
    timestamp: &str,
    nonce: &str,
) -> Result<String, String> {
    let method_ok =
        !method.is_empty() && method.len() <= 10 && method.bytes().all(|b| b.is_ascii_uppercase());
    let path_ok =
        path.starts_with('/') && path.len() <= 2048 && path.bytes().all(|b| b.is_ascii_graphic());
    let timestamp_ok = !timestamp.is_empty()
        && timestamp.len() <= 16
        && timestamp.bytes().all(|b| b.is_ascii_digit());
    if !method_ok || !path_ok || !is_hex(body_sha256, 64) || !timestamp_ok || !is_hex(nonce, 48) {
        return Err("invalid device request signing input".to_string());
    }
    Ok(format!(
        "omnidesk-device-request:v1:{method}:{path}:{body_sha256}:{timestamp}:{nonce}"
    ))
}

#[derive(serde::Deserialize)]
struct PrivateJwk {
    kty: String,
    crv: String,
    d: String,
}

/// Reads the private scalar from a P-256 JWK. Parsed by hand because WebCrypto
/// exports (used by earlier releases) carry extra members such as `ext` and
/// `key_ops` that the strict p256 JWK parser rejects.
fn secret_from_jwk(private_jwk: &str) -> Result<SecretKey, String> {
    let invalid = || "desktop device key is invalid".to_string();
    let jwk: PrivateJwk = serde_json::from_str(private_jwk).map_err(|_| invalid())?;
    if jwk.kty != "EC" || jwk.crv != "P-256" {
        return Err(invalid());
    }
    let scalar = URL_SAFE_NO_PAD
        .decode(jwk.d.trim_end_matches('='))
        .map_err(|_| invalid())?;
    SecretKey::from_slice(&scalar).map_err(|_| invalid())
}

/// ECDSA P-256 / SHA-256 signature as raw r||s (the WebCrypto format the
/// gateway already accepts), base64-encoded.
pub fn sign(private_jwk: &str, message: &str) -> Result<String, String> {
    let secret = secret_from_jwk(private_jwk)?;
    let signature: Signature = SigningKey::from(secret).sign(message.as_bytes());
    Ok(STANDARD.encode(signature.to_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use p256::ecdsa::{signature::Verifier, VerifyingKey};
    use p256::pkcs8::DecodePublicKey;

    const BODY: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
    const NONCE: &str = "0123456789abcdef0123456789abcdef0123456789abcdef";

    fn verify(public_pem: &str, message: &str, signature_b64: &str) -> bool {
        let key = VerifyingKey::from_public_key_pem(public_pem).unwrap();
        let bytes = STANDARD.decode(signature_b64).unwrap();
        let signature = Signature::from_slice(&bytes).unwrap();
        key.verify(message.as_bytes(), &signature).is_ok()
    }

    #[test]
    fn generated_identity_signs_verifiable_requests() {
        let identity = generate_identity().unwrap();
        assert!(identity.device_id.starts_with("desk_") && identity.device_id.len() == 41);
        assert!(identity
            .public_key_pem
            .starts_with("-----BEGIN PUBLIC KEY-----\n"));
        assert!(identity
            .public_key_pem
            .ends_with("-----END PUBLIC KEY-----"));
        let message = request_message("POST", "/app/sync", BODY, "1700000000000", NONCE).unwrap();
        let signature = sign(&identity.private_jwk, &message).unwrap();
        assert_eq!(STANDARD.decode(&signature).unwrap().len(), 64);
        assert!(verify(&identity.public_key_pem, &message, &signature));
    }

    #[test]
    fn keys_exported_by_webcrypto_in_earlier_releases_still_sign() {
        let jwk = r#"{"key_ops":["sign"],"ext":true,"kty":"EC","x":"JBblVPsp6tUEsg_AtQMX0u-_99FoKvQIm1AAqlQG6bA","y":"hCcYmuD220kKL03is16EzaeJEKpYO_QAmQsr3HwbVKw","crv":"P-256","d":"se5PQt3DXStcXCLwcaF5kqmesOJDFZYD6iUo5q12MFQ"}"#;
        // Same 64-column wrapping as the earlier WebCrypto-based pemWrap().
        let pem = "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEJBblVPsp6tUEsg/AtQMX0u+/99Fo\nKvQIm1AAqlQG6bCEJxia4PbbSQovTeKzXoTNp4kQqlg79ACZCyvcfBtUrA==\n-----END PUBLIC KEY-----";
        let message =
            request_message("GET", "/app/bootstrap", BODY, "1700000000000", NONCE).unwrap();
        assert!(verify(pem, &message, &sign(jwk, &message).unwrap()));
    }

    #[test]
    fn rejects_malformed_signing_inputs() {
        assert!(request_message("post", "/a", BODY, "1", NONCE).is_err());
        assert!(request_message("POST", "a", BODY, "1", NONCE).is_err());
        assert!(request_message("POST", "/a b", BODY, "1", NONCE).is_err());
        assert!(request_message("POST", "/a", "zz", "1", NONCE).is_err());
        assert!(request_message("POST", "/a", BODY, "12a", NONCE).is_err());
        assert!(request_message("POST", "/a", BODY, "1", "short").is_err());
        assert!(sign("{}", "message").is_err());
    }
}
