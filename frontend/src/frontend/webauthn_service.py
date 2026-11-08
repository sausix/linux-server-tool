"""Verified, one-use WebAuthn ceremonies bound to server sessions and purposes."""
import json
import secrets
import time
from urllib.parse import urlparse
from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from webauthn import generate_registration_options, generate_authentication_options, verify_registration_response, verify_authentication_response
from webauthn.helpers import options_to_json, bytes_to_base64url, base64url_to_bytes, parse_authentication_credential_json
from webauthn.helpers.structs import PublicKeyCredentialDescriptor, AuthenticatorSelectionCriteria, AuthenticatorAttachment, UserVerificationRequirement, ResidentKeyRequirement
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from frontend.models import AuthChallenge, AuthSession, SecurityKey, UserProfile, utcnow


class WebAuthnService:
    def __init__(self, settings, factory):
        self.settings, self.factory = settings, factory
        origin = urlparse(settings.public_url)
        self.rp_id = origin.hostname.encode('idna').decode('ascii')
        host = '[' + self.rp_id + ']' if ':' in self.rp_id else self.rp_id
        port = origin.port
        self.origin = origin.scheme + '://' + host + (':' + str(port) if port and port != (443 if origin.scheme == 'https' else 80) else '')

    def require_secure_origin(self):
        parsed = urlparse(self.origin)
        if parsed.scheme != 'https' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError('Security keys require an HTTPS public URL (localhost is allowed for development)')

    def profile(self, user, db):
        profile = db.get(UserProfile, user.id)
        if not profile:
            profile = UserProfile(user_id=user.id, webauthn_handle=bytes_to_base64url(secrets.token_bytes(32)))
            db.add(profile)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                profile = db.get(UserProfile, user.id)
        return profile

    def keys(self, user_id, db):
        return list(db.scalars(select(SecurityKey).where(SecurityKey.user_id == user_id).order_by(SecurityKey.created_at, SecurityKey.id)))

    def challenge(self, request, user_id, purpose, payload, lifetime=300):
        row = request.scope.get('session_record')
        if not row:
            raise ValueError('Reload the sign-in or profile page before continuing')
        identity = secrets.token_urlsafe(32)
        with self.factory() as db:
            db.execute(delete(AuthChallenge).where(AuthChallenge.expires_at < time.time()))
            db.execute(update(AuthChallenge).where(AuthChallenge.session_id == row['id'], AuthChallenge.purpose == purpose,
                                                  AuthChallenge.consumed_at.is_(None)).values(consumed_at=time.time()))
            db.add(AuthChallenge(id=identity, session_id=row['id'], user_id=user_id, purpose=purpose,
                                 payload=json.dumps(payload), expires_at=time.time() + lifetime))
            db.commit()
        return identity

    def consume(self, request, user_id, purpose, identity):
        if not isinstance(identity, str) or len(identity) > 64:
            raise ValueError('Invalid or expired security-key challenge')
        with self.factory() as db:
            result = db.execute(update(AuthChallenge).where(AuthChallenge.id == identity,
                AuthChallenge.session_id == (request.scope.get('session_record') or {}).get('id'),
                AuthChallenge.user_id == user_id, AuthChallenge.purpose == purpose, AuthChallenge.expires_at > time.time(),
                AuthChallenge.session_id.in_(select(AuthSession.id).where(AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > time.time(), AuthSession.absolute_expires_at > time.time())),
                AuthChallenge.consumed_at.is_(None)).values(consumed_at=time.time()).returning(AuthChallenge.payload)).scalar_one_or_none()
            db.commit()
        if result is None:
            raise ValueError('Invalid, used or expired security-key challenge')
        return json.loads(result)

    def authentication_options(self, request, user, db, purpose):
        self.require_secure_origin()
        keys = self.keys(user.id, db)
        if not keys:
            raise ValueError('No security key is registered')
        options = generate_authentication_options(rp_id=self.rp_id, user_verification=UserVerificationRequirement.REQUIRED,
            allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(key.credential_id)) for key in keys])
        identity = self.challenge(request, user.id, purpose, {'challenge': bytes_to_base64url(options.challenge)})
        return {'challenge_id': identity, 'publicKey': json.loads(options_to_json(options))}

    def verify_authentication(self, request, user, db, purpose, identity, credential):
        payload = self.consume(request, user.id, purpose, identity)
        parsed = parse_authentication_credential_json(credential)
        key = db.scalar(select(SecurityKey).where(SecurityKey.credential_id == parsed.id, SecurityKey.user_id == user.id))
        if not key:
            raise ValueError('Security key does not belong to this account')
        profile = self.profile(user, db)
        if parsed.response.user_handle is not None and parsed.response.user_handle != base64url_to_bytes(profile.webauthn_handle):
            raise ValueError('Security key user handle does not match')
        verified = verify_authentication_response(credential=parsed, expected_challenge=base64url_to_bytes(payload['challenge']),
            expected_rp_id=self.rp_id, expected_origin=self.origin, credential_public_key=base64url_to_bytes(key.public_key),
            credential_current_sign_count=key.sign_count, require_user_verification=True)
        # CAS prevents concurrent verification from overwriting a newer counter.
        result = db.execute(update(SecurityKey).where(SecurityKey.id == key.id, SecurityKey.sign_count == key.sign_count).values(
            sign_count=verified.new_sign_count, last_used_at=utcnow()))
        if result.rowcount != 1:
            db.rollback(); raise ValueError('Security key changed during verification')
        db.commit()
        return key

    def registration_options(self, request, user, db, name):
        self.require_secure_origin()
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError('Use a security-key name of 1–80 characters')
        profile = self.profile(user, db)
        keys = self.keys(user.id, db)
        if len(keys) >= 20:
            raise ValueError('An account can register at most 20 security keys')
        options = generate_registration_options(rp_id=self.rp_id, rp_name=self.settings.app_name, user_name=user.username,
            user_id=base64url_to_bytes(profile.webauthn_handle), user_display_name=profile.display_name or user.username,
            authenticator_selection=AuthenticatorSelectionCriteria(authenticator_attachment=AuthenticatorAttachment.CROSS_PLATFORM,
                resident_key=ResidentKeyRequirement.DISCOURAGED, user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(key.credential_id)) for key in keys],
            supported_pub_key_algs=[COSEAlgorithmIdentifier.ECDSA_SHA_256, COSEAlgorithmIdentifier.EDDSA, COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256])
        identity = self.challenge(request, user.id, 'register', {'challenge': bytes_to_base64url(options.challenge), 'name': name.strip()})
        return {'challenge_id': identity, 'publicKey': json.loads(options_to_json(options))}

    def verify_registration(self, request, user, db, identity, credential):
        payload = self.consume(request, user.id, 'register', identity)
        verified = verify_registration_response(credential=credential, expected_challenge=base64url_to_bytes(payload['challenge']),
            expected_rp_id=self.rp_id, expected_origin=self.origin, require_user_presence=True, require_user_verification=True)
        if len(verified.credential_id) > 1024 or len(verified.credential_public_key) > 8192:
            raise ValueError('Security key credential exceeds the storage limit')
        transports = credential.get('response', {}).get('transports', [])
        transports = [value for value in transports if value in {'usb', 'nfc', 'ble', 'hybrid', 'internal', 'smart-card'}][:6] if isinstance(transports, list) else []
        key = SecurityKey(user_id=user.id, credential_id=bytes_to_base64url(verified.credential_id),
            public_key=bytes_to_base64url(verified.credential_public_key), sign_count=verified.sign_count,
            name=payload['name'], transports=json.dumps(transports))
        db.add(key)
        try:
            db.commit()
        except IntegrityError:
            db.rollback(); raise ValueError('This security key credential is already registered') from None
        return key
