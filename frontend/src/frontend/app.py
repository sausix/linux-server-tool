from __future__ import annotations

import logging
import hashlib
import asyncio
import json
import time
import shlex
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import Response, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from frontend.sessions import ServerSessionMiddleware, SessionStore
from frontend.audit import SecurityAudit
from frontend.authentication import Authentication
from frontend.login_policy import LoginThrottle, source_ip
from frontend.request_limits import RequestLimitsMiddleware
from frontend.profile_gate import ProfileGateMiddleware
from frontend.webauthn_service import WebAuthnService
from starlette.middleware.trustedhost import TrustedHostMiddleware

from frontend.config import Settings
from frontend.database import Base, build_database, session_dependency
from common.client import DaemonClient, DaemonUnavailable
from common.protocol import ProtocolError
from common.protocol import request as rpc_request, encode_message, MAX_MESSAGE_BYTES
from frontend.models import User
from frontend.inventory import filter_inventory, parse_filters, MODELS
from frontend.network.firewall import tables as firewall_tables
from frontend.server import read_stream_line
from common.ip_validation import validate_ip
from common.display_paths import display_host_path
import secrets
from frontend.security import csrf_is_valid, csrf_token, ensure_admin, verify_credentials

logger = logging.getLogger("frontend")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'self'"
        )
        if not request.url.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store'
        return response


class SchemePolicyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, policy: str):
        super().__init__(app)
        self.policy = policy

    async def dispatch(self, request: Request, call_next):
        if request.url.scheme == "https" or self.policy == "allow":
            return await call_next(request)
        if self.policy == "redirect":
            return RedirectResponse(
                str(request.url.replace(scheme="https")),
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            )
        return JSONResponse({"detail": "HTTPS is required"}, status_code=400)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_sqlite_directory()
    engine, session_factory = build_database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)
        with session_factory() as session:
            initial_password = ensure_admin(session)
        if initial_password:
            logger.warning("Initial admin password: %s", initial_password)
            logger.warning("Sign in as admin and change this temporary password immediately.")
        app.state.shutdown_event = asyncio.Event()
        audit.start()
        try:
            yield {'shutdown_event': app.state.shutdown_event}
        finally:
            app.state.shutdown_event.set()
            audit.close()
            engine.dispose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.image_deletion_previews = {}
    app.state.stack_definition_previews = {}
    app.state.settings = settings
    app.state.session_factory = session_factory
    secret = settings.effective_secret_key()
    if settings.secure_cookie and urlparse(settings.public_url).scheme != 'https':
        logger.warning(settings.format_env_references('Secure session cookies require browser HTTPS, but FRONTEND_PUBLIC_URL uses HTTP. Direct HTTP login will fail; configure public HTTPS or explicitly use FRONTEND_COOKIE_SECURE=auto for an HTTP-only local test.'))
    audit = SecurityAudit(session_factory, settings)
    sessions = SessionStore(session_factory, settings, secret, audit)
    authentication = Authentication(settings, session_factory, sessions, LoginThrottle(session_factory, settings, secret), audit)
    app.state.sessions, app.state.security_audit = sessions, audit
    app.state.authentication = authentication
    app.state.webauthn = WebAuthnService(settings, session_factory)
    daemon_client = DaemonClient(
        settings.daemon_endpoint,
        token=settings.daemon_token,
        timeout=settings.daemon_timeout_seconds,
        method_timeouts={method: settings.daemon_inventory_timeout_seconds for method in (
            'network.snapshot', 'network.routing', 'network.sysctl', 'network.configuration',
            'firewall.snapshot', 'system.snapshot', 'system.sysctl', 'system.configuration')},
    )
    app.state.daemon_client = daemon_client

    package_dir = Path(__file__).parent
    static_version = hashlib.sha256(b''.join(path.read_bytes() for path in sorted((package_dir / 'static').glob('*')) if path.is_file())).hexdigest()[:16]
    templates = Jinja2Templates(directory=package_dir / "templates")
    app.mount("/static", StaticFiles(directory=package_dir / "static"), name="static")

    app.add_middleware(ProfileGateMiddleware, authentication=authentication)
    app.add_middleware(ServerSessionMiddleware, store=sessions)
    app.add_middleware(RequestLimitsMiddleware, settings=settings)
    app.add_middleware(SchemePolicyMiddleware, policy=settings.http_policy)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
    app.add_middleware(SecurityHeadersMiddleware)

    get_db = partial(session_dependency, session_factory)

    from frontend.ui.models import UIModels, color
    presentation_providers, theme = [], {}
    for component in ('network', 'system', 'docker'):
        if getattr(settings, component + '_support'):
            from importlib import import_module
            presentation = import_module('frontend.' + component + '.presentation')
            theme.update(getattr(presentation, 'COLORS', {}))
            if hasattr(presentation, 'extend_interfaces'):
                presentation_providers.append(presentation.extend_interfaces)
                theme['category-' + component] = presentation.ACCENT

    @app.get('/assets/component-theme.css', include_in_schema=False)
    def component_theme():
        return Response(':root{' + ''.join('--' + name + ':' + color(value) + ';' for name, value in theme.items()) + '}', media_type='text/css')

    def project_ui(data):
        ui = UIModels()
        for provider in presentation_providers:
            try:
                provider(data, ui)
            except (ValueError, TypeError, KeyError):
                logger.warning('An optional presentation extension could not be applied')
        data['ui'] = ui.snapshot()
        return data

    def safe_return_path(value):
        parsed = urlparse(value)
        if (parsed.scheme or parsed.netloc or not value.startswith('/') or value.startswith('//')
                or '\\' in value or any(ord(character) < 32 for character in value)):
            return '/'
        if parsed.path.startswith('/docker/stack-models/'):
            return value
        if parsed.path in {'/settings', '/settings/user', '/settings/security', '/system', '/system/sysctl', '/system/configuration', '/docker/volumes', '/docker/volumes/detail'}:
            return value
        if parsed.path not in {'/', '/network', '/docker', '/docker/configuration', '/docker/labels', '/password'} and not parsed.path.startswith(('/network/', '/docker/stacks', '/docker/containers', '/docker/images', '/docker/resources/', '/docker/stack-definitions/', '/docker/stack-files/', '/docker/stack-actions/', '/docker/stack-jobs/')):
            return '/'
        return value

    @app.exception_handler(StarletteHTTPException)
    async def authentication_errors(request: Request, exc: StarletteHTTPException):
        if exc.headers and exc.headers.get('X-Profile-Required') == 'true' and not request.url.path.startswith('/api/'):
            return RedirectResponse('/settings/user', status_code=303, headers={'Cache-Control': 'no-store'})
        if exc.status_code == 401 and not request.url.path.startswith('/api/'):
            from urllib.parse import urlencode
            target = safe_return_path(request.url.path + ('?' + request.url.query if request.url.query else ''))
            return RedirectResponse('/login?' + urlencode({'reason': 'session', 'next': target}), status_code=303,
                                    headers={'Cache-Control': 'no-store'})
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # Never echo passwords, credentials, uploaded contents or input values.
        return JSONResponse({'detail': 'Invalid request fields'}, status_code=422)

    def current_user(request: Request, db: Session) -> User | None:
        return authentication.current_user(request, db)

    @app.get('/api/session')
    def session_status(request: Request, db: Session = Depends(get_db)):
        if not current_user(request, db):
            raise HTTPException(401, 'Sign in required')
        return JSONResponse({'authenticated': True}, headers={'Cache-Control': 'no-store'})

    @app.post('/api/session/refresh')
    def set_live_refresh(request: Request, enabled: bool = Form(...),
                         csrf: str = Form(..., alias='csrf_token'), db: Session = Depends(get_db)):
        if not current_user(request, db):
            raise HTTPException(401, 'Sign in required')
        if not csrf_is_valid(request.session, csrf):
            raise HTTPException(403, 'Invalid CSRF token')
        request.session['live_refresh'] = enabled
        return JSONResponse({'enabled': enabled}, headers={'Cache-Control': 'no-store'})

    def render(request: Request, name: str, **context):
        if name == 'login.html':
            context['cookie_warning'] = (
                'This request is recognized as HTTP, but session cookies require HTTPS. '
                'For direct HTTP testing, use an HTTP FRONTEND_PUBLIC_URL and FRONTEND_COOKIE_SECURE=auto. '
                'For public access, use HTTPS; behind a reverse proxy, verify trusted HTTPS forwarding.'
                if settings.secure_cookie and request.url.scheme != 'https' else None)
        if 'daemon_status' in context:
            context['daemon_status'] = component_status(context['daemon_status'])
        if context.get('cookie_warning'):
            context['cookie_warning'] = settings.format_env_references(context['cookie_warning'])
        context['settings_mode'] = request.url.path.startswith('/settings') or request.url.path == '/password'
        context['profile_restricted'] = request.scope.get('password_change_required') or request.scope.get('mfa_enrollment_required')
        if context['settings_mode']:
            context.setdefault('daemon_status', {'available': False})
        if context.get('user') and 'daemon_status' not in context:
            context['daemon_status'] = service_status()
        context['search_query'] = ' '.join(
            value if key == 'q' else shlex.quote(f'{key}={value}')
            for key, value in request.query_params.multi_items()
            if (value or key == 'label.value') and (key in {'q', 'class', 'prefix'} or any(key.startswith(prefix + '.') for prefix in MODELS))
        )
        if context.get('daemon_status'):
            capabilities = context['daemon_status']
            capabilities.setdefault('features', {feature: 'disabled' if capabilities.get('network_access', 'disabled') == 'disabled' else 'ro'
                                                for feature in ('interfaces', 'routing', 'firewall', 'sysctl')})
            network_write = capabilities.get('network_access') in {'rw', 'readwrite'} and any(
                capabilities['features'].get(feature) in {'rw', 'readwrite'} for feature in ('interfaces', 'routing', 'firewall'))
            context['network_title'] = 'Network control' if network_write else 'Network monitoring'
            context['docker_title'] = 'Docker control' if capabilities.get('docker_access') in {'rw', 'readwrite'} else 'Docker monitoring'
            path = request.url.path
            access = None
            if path == '/network' or path.startswith('/network/'):
                feature = path.split('/')[2] if path.startswith('/network/') else None
                writable = network_write if feature is None else (
                    capabilities.get('network_access') in {'rw', 'readwrite'} and
                    capabilities['features'].get(feature) in {'rw', 'readwrite'})
                access = 'Write permission' if writable else 'Read only'
            elif path == '/system' or path.startswith('/system/'):
                access = 'Write permission' if capabilities.get('system_access') in {'rw', 'readwrite'} and (path == '/system' or capabilities['features'].get('sysctl') == 'rw') else 'Read only'
            elif path == '/docker' or path.startswith('/docker/'):
                access = 'Write permission' if capabilities.get('docker_access') in {'rw', 'readwrite'} else 'Read only'
            context['page_access'] = access
            if path in {'/system/configuration', '/network/configuration'}:
                context['page_access'] = 'Read only'
        context['extension_topics'] = ['docker'] if settings.docker_support and context.get('daemon_status', {}).get('docker_access', 'disabled') != 'disabled' else []
        context['daemon_endpoint_display'] = safe_endpoint(settings.daemon_endpoint)
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={"app_name": settings.app_name, "display_path": partial(display_host_path, mappings=context.get('daemon_status', {}).get('host_paths', {})), "static_version": static_version, "csrf_token": csrf_token(request.session), 'live_refresh': request.session.get('live_refresh', True), 'component_support': {name: getattr(settings, name + '_support') for name in ('docker', 'network', 'system')},
                     'session_renew_seconds': min(60, settings.session_max_age_seconds // 3), **context},
        )

    def component_status(value):
        value = dict(value)
        for component in ('docker', 'network', 'system'):
            if not getattr(settings, component + '_support'):
                value[component + '_access'] = 'disabled'
        return value

    def service_status():
        try:
            return component_status({**daemon_client.ping(), 'available': True})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            return {'available': False, 'message': str(exc)}

    def safe_endpoint(endpoint):
        from urllib.parse import urlsplit, urlunsplit
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if parsed.port:
            host += ':' + str(parsed.port)
        return urlunsplit((parsed.scheme, host, parsed.path, '', ''))

    def require_system(request, db, feature=None):
        if not settings.system_support:
            raise HTTPException(404, "System support is disabled")
        user = current_user(request, db)
        if not user:
            raise HTTPException(401, 'Sign in required')
        capabilities = service_status()
        if not capabilities.get('available'):
            raise HTTPException(503, 'backend is unavailable')
        if capabilities.get('system_access', 'readonly') == 'disabled' or (feature and capabilities.get('features', {}).get(feature) == 'disabled'):
            raise HTTPException(403, 'System feature is disabled')
        return user, capabilities

    def require_network(request, db, feature=None):
        if feature == 'sysctl':
            return require_system(request, db, feature)
        if not settings.network_support:
            raise HTTPException(404, "Network support is disabled")
        user = current_user(request, db)
        if not user:
            raise HTTPException(401, 'Sign in required')
        capabilities = service_status()
        if not capabilities.get('available'):
            raise HTTPException(503, 'backend is unavailable')
        if capabilities.get('network_access', 'disabled') == 'disabled':
            raise HTTPException(403, 'Network access is disabled')
        if feature and capabilities.get('features', {}).get(feature) == 'disabled':
            raise HTTPException(403, f'{feature} feature is disabled')
        return user, capabilities

    require_docker = None
    if settings.docker_support:
        from frontend.docker.routes import install as install_docker
        require_docker = install_docker(app, settings, daemon_client, render, get_db, current_user, service_status, safe_return_path)

    @app.get('/system/configuration', response_class=HTMLResponse)
    @app.get('/network/configuration', response_class=HTMLResponse)
    def configuration_page(request: Request, db: Session = Depends(get_db)):
        category = request.url.path.split('/')[1]
        user, capabilities = require_system(request, db) if category == 'system' else require_network(request, db)
        return render(request, 'configuration_review.html', user=user, daemon_status=capabilities,
                      category=category, hide_search=True)

    @app.get('/api/system/configuration')
    @app.get('/api/network/configuration')
    def configuration_data(request: Request, db: Session = Depends(get_db)):
        category = request.url.path.split('/')[2]
        if category == 'system':
            require_system(request, db)
        else:
            require_network(request, db)
        try:
            return JSONResponse(daemon_client.call(category + '.configuration'), headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get('/system', response_class=HTMLResponse)
    def system_page(request: Request, db: Session = Depends(get_db)):
        user, capabilities = require_system(request, db)
        return render(request, 'system.html', user=user, daemon_status=capabilities, hide_search=True)

    @app.get('/api/system')
    def system_data(request: Request, db: Session = Depends(get_db)):
        require_system(request, db)
        try:
            return JSONResponse(daemon_client.call('system.snapshot'), headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get('/system/sysctl', response_class=HTMLResponse)
    def system_sysctl(request: Request, db: Session = Depends(get_db)):
        user, capabilities = require_system(request, db, 'sysctl')
        fields = [f'{prefix}.{field}' for prefix in ['interface', 'sysctl'] for field in MODELS[prefix].model_fields] + ['class', 'prefix']
        return render(request, 'network.html', user=user, daemon_status=capabilities, view='sysctl',
                      search_action='/system/sysctl', search_fields=fields, show_search=True)

    @app.get('/api/system/sysctl')
    def system_sysctl_data(request: Request, db: Session = Depends(get_db)):
        return network_data('sysctl', request, db)

    @app.get('/network', response_class=HTMLResponse)
    def network_overview(request: Request, db: Session = Depends(get_db)):
        user, capabilities = require_network(request, db)
        return render(request, 'network_overview.html', user=user,
                      daemon_status=capabilities, hide_search=True)

    @app.get('/network/{view}', response_class=HTMLResponse)
    def network_page(view: str, request: Request, db: Session = Depends(get_db)):
        if view == 'sysctl':
            require_system(request, db, 'sysctl')
            return RedirectResponse('/system/sysctl' + ('?' + request.url.query if request.url.query else ''), status_code=303)
        if view not in {'interfaces', 'routing', 'firewall', 'sysctl'}:
            raise HTTPException(404)
        user, capabilities = require_network(request, db, view)
        prefixes = {'interfaces': ['interface'], 'routing': ['interface', 'route'], 'sysctl': ['interface', 'sysctl'], 'firewall': ['firewall']}[view]
        fields = [f'{prefix}.{field}' for prefix in prefixes for field in MODELS[prefix].model_fields]
        if view == 'sysctl':
            fields += ['class', 'prefix']
        return render(request, 'network.html', user=user, daemon_status=capabilities, view=view, search_fields=fields)

    @app.get('/api/network/{view}')
    def network_data(view: str, request: Request, db: Session = Depends(get_db)):
        _, capabilities = require_network(request, db, 'interfaces' if view == 'overview' else view)
        methods = {'overview': 'network.snapshot', 'interfaces': 'network.snapshot', 'routing': 'network.routing', 'sysctl': 'system.sysctl' if request.url.path.startswith('/api/system') else 'network.sysctl', 'firewall': 'firewall.snapshot'}
        if view not in methods:
            raise HTTPException(404)
        parameters = [] if view == 'overview' else list(request.query_params.multi_items())
        try:
            parse_filters(parameters)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            data = project_ui(daemon_client.call(methods[view]))
            if view == 'overview':
                data = filter_inventory('interfaces', data, [])
                return JSONResponse({'ui': data['ui'], 'interfaces': [
                    {key: item.get(key) for key in ('name', 'mac_address', 'operational_state', 'scope', 'kind', 'master')}
                    for item in data.get('interfaces', [])
                ]}, headers={'Cache-Control': 'no-store'})
            interfaces = None
            if view in {'routing', 'sysctl'} and capabilities.get('features', {}).get('interfaces') != 'disabled' and any(key.startswith('interface.') or 'interface.' in value for key, value in parameters):
                interfaces = daemon_client.call('network.snapshot')
            filtered = filter_inventory(view, data, parameters, interfaces)
            if view == 'firewall':
                full_tables = firewall_tables(data)
                filtered = dict(filtered)
                named = {(item['counter'].get('family'), item['counter'].get('table'), item['counter'].get('name')): item['counter']
                         for item in (data.get('ruleset') or {}).get('nftables', []) if 'counter' in item}
                filtered['tables'] = firewall_tables(filtered, named)
                known = {(table['backend'], table['family'], table['name']): {chain['name'] for chain in table['chains']} for table in full_tables}
                for table in filtered['tables']:
                    names = known.get((table['backend'], table['family'], table['name']), set())
                    for chain in table['chains']:
                        for rule in chain['rules']:
                            rule['jump_targets'] = rule['targets'] if table['backend'] == 'nftables' else [target for target in rule['targets'] if target in names]
            return JSONResponse(filtered, headers={'Cache-Control': 'no-store'})
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            detail = f'{methods[view]} failed ({type(exc).__name__}): {exc}'
            logger.warning('backend inventory request failed: %s', detail)
            raise HTTPException(503, detail) from exc

    def require_address_write(request, db):
        user, capabilities = require_network(request, db, 'interfaces')
        if capabilities.get('features', {}).get('interfaces') != 'rw' or capabilities.get('network_access') not in {'rw', 'readwrite'}:
            raise HTTPException(403, 'Interfaces are read-only')
        if not user.is_admin or user.password_change_required():
            raise HTTPException(403, 'An administrator with an up-to-date password is required')
        return user, capabilities

    def check_interface_address(interface, address, action):
        inventory = daemon_client.call('network.snapshot')
        if not any(item['name'] == interface for item in inventory['interfaces']):
            raise ValueError('Interface no longer exists.')
        from ipaddress import ip_interface
        present = any(item['interface'] == interface and
                      ip_interface(f"{item['address']}/{item['prefix_length']}") == ip_interface(address)
                      for item in inventory['addresses'])
        if action in {'delete', 'persist'} and not present:
            raise ValueError('This runtime address no longer exists.')
        if action == 'add' and present:
            raise ValueError('This address is already assigned.')

    @app.get('/api/address/validate')
    def address_validation(request: Request, address: str, prefix_policy: str = 'required', db: Session = Depends(get_db)):
        require_network(request, db, 'interfaces')
        try:
            return JSONResponse(validate_ip(address, prefix_policy), headers={'Cache-Control': 'no-store'})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get('/network/interfaces/address/{action}', response_class=HTMLResponse)
    def address_page(action: str, request: Request, interface: str, address: str = '', db: Session = Depends(get_db)):
        user, capabilities = require_address_write(request, db)
        if action not in {'add', 'delete', 'persist', 'remove-config'}:
            raise HTTPException(404)
        info, confirmation, error, configuration_preview = None, None, None, None
        if action != 'add':
            try:
                info = validate_ip(address, 'required')
                check_interface_address(interface, info['address'], action)
                if action in {'persist', 'remove-config'}:
                    configuration_preview = daemon_client.call('network.address.configuration', {
                        'interface': interface, 'address': info['address'], 'action': action, 'apply': False})
                confirmation = secrets.token_urlsafe(32)
                request.session['address_change'] = {'token': confirmation, 'interface': interface,
                    'address': info['address'], 'action': action}
                if configuration_preview:
                    request.session['address_change']['expected_hash'] = configuration_preview['content_hash']
            except (ValueError, DaemonUnavailable, ProtocolError) as exc:
                error = str(exc)
        return render(request, 'address.html', user=user, daemon_status=capabilities,
                      view='interfaces', action=action, interface=interface, address=address,
                      info=info, confirmation=confirmation, error=error, configuration_preview=configuration_preview)

    @app.post('/network/interfaces/address/{action}', response_class=HTMLResponse)
    def address_change(action: str, request: Request, interface: str = Form(...), address: str = Form(...),
                       csrf: str = Form(..., alias='csrf_token'), confirmation: str = Form(''),
                       db: Session = Depends(get_db)):
        user, capabilities = require_address_write(request, db)
        if not csrf_is_valid(request.session, csrf):
            raise HTTPException(403, 'Invalid CSRF token')
        if action not in {'add', 'delete', 'persist', 'remove-config'}:
            raise HTTPException(404)
        info, next_confirmation, error, configuration_preview = None, None, None, None
        try:
            info = validate_ip(address, 'required')
            check_interface_address(interface, info['address'], action)
            params = {'action': action, 'interface': interface, 'address': info['address']}
            if confirmation:
                pending = request.session.pop('address_change', {})
                expected_hash = pending.pop('expected_hash', None)
                if pending != {'token': confirmation, **params}:
                    raise HTTPException(409, 'Confirmation expired or does not match this change. Preview again.')
                method = 'network.address.configuration' if action in {'persist', 'remove-config'} else 'network.address.change'
                daemon_client.call(method, {**params, 'apply': True, **({'expected_hash': expected_hash} if expected_hash else {})})
                from urllib.parse import urlencode
                return RedirectResponse('/network/interfaces?' + urlencode({'interface.name': interface}), status_code=303)
            method = 'network.address.configuration' if action in {'persist', 'remove-config'} else 'network.address.change'
            preview = daemon_client.call(method, {**params, 'apply': False})
            if action in {'persist', 'remove-config'}:
                configuration_preview = preview
            next_confirmation = secrets.token_urlsafe(32)
            request.session['address_change'] = {'token': next_confirmation, **params}
            if configuration_preview:
                request.session['address_change']['expected_hash'] = configuration_preview['content_hash']
        except (ValueError, DaemonUnavailable, ProtocolError) as exc:
            error = str(exc)
        return render(request, 'address.html', user=user, daemon_status=capabilities,
                      view='interfaces', action=action, interface=interface, address=address,
                      info=info, confirmation=next_confirmation, error=error, configuration_preview=configuration_preview)

    @app.get('/api/events')
    def live_events(request: Request, component: str = 'network', db: Session = Depends(get_db)):
        if component not in {'network', 'docker', 'system'} or not getattr(settings, component + '_support', False):
            raise HTTPException(422, 'Unknown event component')
        user, capabilities = require_docker(request, db) if component == 'docker' else require_system(request, db, 'sysctl') if component == 'system' else require_network(request, db)
        if not request.session.get('live_refresh', True):
            raise HTTPException(409, 'Live refresh is paused for this session')
        if not capabilities.get('events'):
            raise HTTPException(409, 'Live events are disabled')

        async def stream():
            writer = None
            try:
                endpoint = urlparse(settings.daemon_endpoint)
                if endpoint.scheme == 'unix':
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_unix_connection(endpoint.path, limit=MAX_MESSAGE_BYTES + 1),
                        settings.daemon_timeout_seconds,
                    )
                else:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(endpoint.hostname, endpoint.port, limit=MAX_MESSAGE_BYTES + 1),
                        settings.daemon_timeout_seconds,
                    )
                topics = ['network.routing', 'network.config'] if component == 'system' else [component]
                if component == 'docker' and capabilities.get('network_access') in {'rw', 'ro', 'readwrite', 'readonly'} and capabilities.get('features', {}).get('interfaces') in {'rw', 'ro'}:
                    topics.append('network.link')
                writer.write(encode_message(rpc_request('events.subscribe', {'topics': topics}, settings.daemon_token)))
                await writer.drain()
                line = await read_stream_line(reader, app.state.shutdown_event, settings.daemon_timeout_seconds, cancel_event=request.scope['stream_session_end'])
                if line is None:
                    yield ('event: session-expired\ndata: {}\n\n' if request.scope['stream_session_end'].is_set() else 'event: shutdown\ndata: {}\n\n')
                    return
                acknowledgement = json.loads(line)
                if 'error' in acknowledgement:
                    raise ValueError('Subscription rejected')
                yield 'event: ready\ndata: {}\n\n'
                while True:
                    try:
                        line = await read_stream_line(reader, app.state.shutdown_event, 15, cancel_event=request.scope['stream_session_end'])
                    except TimeoutError:
                        yield ': heartbeat\n\n'
                        continue
                    if line is None:
                        yield ('event: session-expired\ndata: {}\n\n' if request.scope['stream_session_end'].is_set() else 'event: shutdown\ndata: {}\n\n')
                        break
                    if not line:
                        break
                    message = json.loads(line)
                    if message.get('method') == 'event':
                        yield 'event: change\ndata: ' + json.dumps(message.get('params', {})) + '\n\n'
            except (OSError, ValueError, TimeoutError):
                yield 'event: unavailable\ndata: {}\n\n'
            finally:
                if writer:
                    writer.close()
                    try:
                        await asyncio.wait_for(writer.wait_closed(), settings.daemon_timeout_seconds)
                    except (OSError, TimeoutError):
                        pass

        db.close()
        async def bound_stream():
            row = request.scope['session_record']
            async with sessions.monitor(row['id'], user.id) as ended:
                request.scope['stream_session_end'] = ended
                async for chunk in stream():
                    yield chunk
        return StreamingResponse(bound_stream(), media_type='text/event-stream', headers={
            'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no',
        })

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, db: Session = Depends(get_db)):
        if current_user(request, db):
            return RedirectResponse("/", status_code=303)
        if request.session.get('user_id'):
            request.session.clear()
        if request.query_params.get('restart') == '1':
            for key in ('mfa_user', 'password_proof', 'password_verified_at', 'return_to'):
                request.session.pop(key, None)
        pending_mfa = bool(request.session.get('mfa_user') and time.time() - request.session.get('password_verified_at', 0) < 300)
        return render(request, "login.html", error=None,
                      pending_mfa=pending_mfa, return_to=safe_return_path(request.query_params.get('next', '/')),
                      session_notice=request.query_params.get('reason') == 'session')

    @app.post("/login", response_class=HTMLResponse)
    def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        submitted_csrf: str = Form(..., alias="csrf_token"),
        return_to: str = Form('/', alias='next'),
        db: Session = Depends(get_db),
    ):
        if not csrf_is_valid(request.session, submitted_csrf):
            missing = not request.scope.get('session_record')
            audit.emit('login.denied', request, reason='missing_session' if missing else 'invalid_csrf', outcome='denied')
            message = ('The sign-in session is missing or expired. Allow cookies and reload the sign-in page.'
                       if missing else 'The sign-in form expired or changed. Review the page and try again.')
            response = render(request, 'login.html', error=message, pending_mfa=False,
                              return_to=safe_return_path(return_to), session_notice=False)
            response.status_code = 403
            return response
        username = username.strip().lower()
        mode = authentication.guard_login(username[:64], request)
        if not authentication.throttle.slots.acquire(blocking=False):
            audit.emit('login.throttled', request, username=username[:64], reason='server_busy', outcome='blocked')
            raise HTTPException(429, 'Sign-in capacity is busy. Try again shortly.', headers={'Retry-After': '2'})
        try:
            user = verify_credentials(db, username, password) if len(username) <= 64 and len(password) <= 256 else None
        finally:
            authentication.throttle.slots.release()
        if not user:
            authentication.failed(username[:64], request)
            return render(request, 'login.html', error='Invalid username or password.',
                          return_to=safe_return_path(return_to), session_notice=False)
        audit.emit('login.password_verified', request, user, login_method='password', outcome='success')
        keys = app.state.webauthn.keys(user.id, db)
        if mode != 'off' and keys:
            pending = {'csrf_token': secrets.token_urlsafe(32), 'mfa_user': user.id,
                       'password_proof': authentication.password_fingerprint(user), 'password_verified_at': time.time(),
                       'return_to': safe_return_path(return_to)}
            old = request.scope.get('session_record')
            cookie, data, row = sessions.create(None, pending, source_ip(request), request.headers.get('user-agent', ''), 'password-pending')
            if old:
                sessions.revoke(old['id'], reason='session_rotated', request=request)
            request.session.clear(); request.session.update(data)
            request.scope.update(session_record=row, new_session_cookie=cookie)
            return RedirectResponse('/login?step=security-key', status_code=303)
        try:
            target = authentication.complete(user, request, 'password-enrollment' if mode == 'required' else 'password')
        except ValueError:
            raise HTTPException(401, 'Credentials changed; sign in again') from None
        return RedirectResponse(target if target != '/' else safe_return_path(return_to), status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, db: Session = Depends(get_db)):
        user = current_user(request, db)
        if not user:
            raise HTTPException(401, 'Sign in required')
        try:
            daemon_status = component_status(daemon_client.ping())
        except (DaemonUnavailable, ProtocolError, ValueError) as exc:
            daemon_status = {"available": False, "message": str(exc)}
        else:
            daemon_status["available"] = True
        return render(
            request,
            "index.html",
            user=user,
            hide_search=True,
            password_change_required=user.password_change_required(),
            daemon_status=daemon_status,
        )

    @app.post("/logout")
    def logout(request: Request, submitted_csrf: str = Form(..., alias="csrf_token")):
        if not csrf_is_valid(request.session, submitted_csrf):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
        row = request.scope.get('session_record')
        if row:
            sessions.revoke(row['id'], request=request)
        request.session.clear(); request.scope['destroy_session'] = True
        return RedirectResponse("/login", status_code=303)

    from frontend.account_routes import install_account_routes
    install_account_routes(app, render, get_db, current_user, safe_return_path)
    return app

