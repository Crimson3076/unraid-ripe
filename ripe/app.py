"""LAN-only Stage 1 API and pages. No install/update/exec endpoints exist."""
from __future__ import annotations
import asyncio
import os
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import time
from fastapi import FastAPI, HTTPException, Request, Body
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from . import __version__
from .config import Runtime, checked_url
from .models import Settings, Profile
from .service import Coordinator
from .intelligence import sanitize_profile, local_model_url
from .autoprofile import edited_origin
from .notifications import Notifier, is_quiet

PACKAGE = Path(__file__).parent


def validate_settings(data: dict, current: Settings) -> Settings:
    if set(data) - set(Settings.model_fields) or 'demo_mode' in data:
        raise ValueError('Unknown or read-only setting')
    settings = Settings.model_validate({**current.model_dump(), **data})
    try:
        ZoneInfo(settings.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError('Choose an IANA timezone') from None
    for field in ('quiet_start','quiet_end','digest_time'):
        value = getattr(settings,field)
        if not re.fullmatch(r'\d{2}:\d{2}', value):
            raise ValueError('Times must use HH:MM')
        time.fromisoformat(value)
    if set(settings.soak_days) != {'infrastructure','internet','general'} or any(type(v) is not int or not 0 <= v <= 365 for v in settings.soak_days.values()):
        raise ValueError('Set soak days for all three tiers (0–365)')
    if settings.ollama_url:
        local_model_url(settings.ollama_url)
    checked_url(settings.dashboard_url)
    if len(settings.notification_people)>20 or any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',p) for p in settings.notification_people):
        raise ValueError('Invalid notification recipients')
    if settings.notification_script and not re.fullmatch(r'script\.[a-z0-9_]{1,100}',settings.notification_script):
        raise ValueError('Use a Home Assistant script entity ID')
    if settings.notifications_enabled and not settings.notification_script:
        raise ValueError('Configure a Home Assistant script before enabling notifications')
    for path in (settings.npm_database_path,settings.cloudflared_config_path):
        if path and (not re.fullmatch(r'/[\w.\- /]{1,300}',path) or '..' in path.split('/')):
            raise ValueError('Use an absolute path to a mounted file')
    if len(settings.notification_channel)>100:
        raise ValueError('Notification channel is too long')
    if any(not re.fullmatch(r'[a-zA-Z0-9_.:/-]{1,100}',m) for m in (settings.local_model,settings.gemini_model)):
        raise ValueError('Invalid model name')
    return settings


def create_app(runtime: Runtime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager = Coordinator(runtime or Runtime())
        app.state.manager = manager
        if not manager.runtime.disable_scheduler:
            manager.task = asyncio.create_task(manager.run())
        try:
            yield
        finally:
            manager.closing = True
            for task in (manager.task,manager.poll_task,manager.classify_task):
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            manager.close()
    app = FastAPI(title='Ripe',version=__version__,lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({'detail':'Invalid request'}, status_code=422)
    allowed = os.getenv('RIPE_ALLOWED_HOSTS','localhost,127.0.0.1,::1').split(',')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[h.strip() for h in allowed])

    @app.middleware('http')
    async def protect(request: Request, call_next):
        try:
            if int(request.headers.get('content-length', '0')) > 262144:
                return JSONResponse({'detail':'Request too large'}, status_code=413)
        except ValueError:
            return JSONResponse({'detail':'Invalid request'}, status_code=400)
        if request.method not in ('GET','HEAD','OPTIONS'):
            if request.headers.get('x-ripe-request') != '1':
                return JSONResponse({'detail':'A same-origin Ripe request is required'}, status_code=403)
            origin = request.headers.get('origin')
            if origin and urlsplit(origin).netloc.lower() != request.headers.get('host','').lower():
                return JSONResponse({'detail':'Cross-origin changes are blocked'},status_code=403)
            if request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail':'Cross-site changes are blocked'},status_code=403)
        response = await call_next(request)
        response.headers.update({'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            'Cache-Control':'no-store'})
        return response

    app.mount('/static',StaticFiles(directory=PACKAGE/'static'),name='static')
    templates = Jinja2Templates(directory=PACKAGE/'templates')
    templates.env.globals['ripe_version'] = __version__

    @app.get('/health')
    async def health():
        return {'status':'ok','stage':1,'read_only':True,**app.state.manager.queue_progress(),'polling':app.state.manager.polling}

    @app.get('/')
    async def home(request:Request):
        return templates.TemplateResponse(request=request,name='dashboard.html')

    @app.get('/apps/{app_id}')
    async def detail(request:Request, app_id:str):
        if not request.app.state.manager.store.app(app_id):
            raise HTTPException(404,'App not found')
        return templates.TemplateResponse(request=request,name='app.html')

    @app.get('/settings')
    async def settings_page(request:Request):
        return templates.TemplateResponse(request=request,name='settings.html')

    @app.get('/logs')
    async def logs_page(request:Request):
        return templates.TemplateResponse(request=request,name='logs.html')

    @app.get('/api/dashboard')
    async def dashboard(request:Request):
        return request.app.state.manager.dashboard()

    @app.get('/api/apps/{app_id}')
    async def app_detail(request:Request,app_id:str):
        view = request.app.state.manager.view(app_id)
        if view is None:
            raise HTTPException(404,'App not found')
        return view

    @app.patch('/api/apps/{app_id}/profile')
    async def profile(request:Request,app_id:str,data:dict=Body(...)):
        manager = request.app.state.manager
        app_record = manager.store.app(app_id)
        if not app_record:
            raise HTTPException(404,'App not found')
        if set(data)-set(Profile.model_fields):
            raise HTTPException(422,'Unknown profile field')
        try:
            profile = sanitize_profile(Profile.model_validate({**app_record.profile.model_dump(),**data,'origin':app_record.profile.origin}))
        except ValueError:
            raise HTTPException(422,'Invalid profile') from None
        # Whatever the user changed is theirs from now on; later scans leave it alone.
        profile.origin = edited_origin(app_record.profile,profile)
        manager.mark_changed(app_id)
        manager.store.set('profile:'+app_id,profile.model_dump(mode='json'))
        app_record.profile = profile
        manager.store.save_app(app_record)
        # Profile edits invalidate exclusions immediately. A future local analysis
        # must re-establish both citations before dismissing any risk.
        from .intelligence.evidence import conservative_applicability
        for release in manager.store.releases(app_id):
            release.applicability = conservative_applicability(release.classification)
            manager.store.save_release(release)
        manager.request_poll()
        return {'profile':profile,'sanitized':True}

    def ignore_scope(data: dict) -> str:
        scope = data.get('scope')
        if scope not in ('app', 'version'):
            raise HTTPException(422, 'Choose to ignore the app or its current version')
        return scope

    @app.post('/api/apps/{app_id}/ignore')
    async def ignore(request: Request, app_id: str, data: dict = Body(...)):
        try:
            record = request.app.state.manager.set_ignore(app_id, ignore_scope(data), True)
        except KeyError:
            raise HTTPException(404, 'App not found') from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {'ignored': record.ignored, 'ignored_version': record.ignored_version}

    @app.delete('/api/apps/{app_id}/ignore')
    async def unignore(request: Request, app_id: str, scope: str = ''):
        try:
            record = request.app.state.manager.set_ignore(app_id, ignore_scope({'scope': scope}), False)
        except KeyError:
            raise HTTPException(404, 'App not found') from None
        return {'ignored': record.ignored, 'ignored_version': record.ignored_version}

    @app.post('/api/auto-profiles/review')
    async def review_auto_profiles(request:Request):
        request.app.state.manager.review_auto_profiles()
        return {'reviewed':True}

    @app.patch('/api/apps/{app_id}/mapping')
    async def mapping(request:Request,app_id:str,data:dict=Body(...)):
        manager=request.app.state.manager
        record=manager.store.app(app_id)
        if record is None:
            raise HTTPException(404,'App not found')
        repo=data.get('upstream_repo')
        if repo is not None and not isinstance(repo, str):
            raise HTTPException(422,'Use a GitHub owner/repository mapping')
        if repo:
            repo=repo.removeprefix('https://github.com/').removesuffix('.git').strip('/')
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) or any(p in ('.','..') for p in repo.split('/')):
                raise HTTPException(422,'Use a GitHub owner/repository mapping')
        manager.mark_changed(app_id)
        record.upstream_repo=repo or None
        manager.store.set('mapping:'+app_id,{'upstream_repo':record.upstream_repo})
        manager.store.save_app(record)
        # A changed mapping must not keep claiming risks from the old project.
        with manager.store.lock,manager.store.db:
            manager.store.db.execute('DELETE FROM releases WHERE app_id=?',(app_id,))
        manager.request_poll()
        return {'upstream_repo':record.upstream_repo}

    @app.get('/api/settings')
    async def settings(request:Request):
        return request.app.state.manager.settings()

    @app.get('/api/connections/home-assistant')
    async def ha_connection(request: Request):
        manager = request.app.state.manager
        if manager.demo:
            return {'url': '', 'has_token': False, 'configured': False, 'source': 'demo', 'last_test': None, 'demo': True}
        return {**manager.ha_connection.public(), 'demo': False}

    @app.put('/api/connections/home-assistant')
    async def save_ha_connection(request: Request, data: dict = Body(...)):
        manager = request.app.state.manager
        if manager.demo:
            raise HTTPException(409, 'Leave demo mode before configuring the live Home Assistant connection')
        try:
            return manager.save_ha_connection(data)
        except ValueError:
            raise HTTPException(422, 'Invalid Home Assistant connection: use a valid server URL and long-lived token; changing servers requires a new token or removal of the saved token') from None
        except OSError:
            raise HTTPException(500, 'Could not save the private Home Assistant settings; check /config permissions') from None

    @app.delete('/api/connections/home-assistant')
    async def disconnect_ha(request: Request):
        manager = request.app.state.manager
        if manager.demo:
            raise HTTPException(409, 'Leave demo mode before changing the live Home Assistant connection')
        try:
            return manager.save_ha_connection(disconnect=True)
        except (ValueError, OSError):
            raise HTTPException(500, 'Could not remove the private Home Assistant connection; check /config permissions') from None

    @app.post('/api/connections/home-assistant/test')
    async def test_ha_connection(request: Request):
        manager = request.app.state.manager
        if manager.demo:
            raise HTTPException(409, 'Demo mode never connects to your live Home Assistant')
        try:
            return await manager.ha_connection.test()
        except ValueError:
            raise HTTPException(409, 'Save a valid Home Assistant connection in Settings, then test it again') from None

    def credential_routes(name, label):
        route = '/api/connections/' + name

        @app.get(route, name=name + '_get')
        async def get_credential(request: Request):
            manager = request.app.state.manager
            if manager.demo:
                return {'source': 'demo', 'configured': False, 'status': 'Not set', 'last_test': None, 'demo': True}
            return {**manager.api_credentials[name].public(), 'demo': False}

        @app.put(route, name=name + '_put')
        async def put_credential(request: Request, data: dict = Body(...)):
            manager = request.app.state.manager
            if manager.demo:
                raise HTTPException(409, f'Leave demo mode before configuring the live {label}')
            try:
                return {**manager.change_api_credential(name, data), 'demo': False}
            except ValueError:
                raise HTTPException(422, f'Invalid {label}: use a value without spaces or control characters') from None
            except OSError:
                raise HTTPException(500, f'Could not save the private {label}; check /config permissions') from None

        @app.delete(route, name=name + '_delete')
        async def delete_credential(request: Request):
            manager = request.app.state.manager
            if manager.demo:
                raise HTTPException(409, f'Leave demo mode before changing the live {label}')
            try:
                return {**manager.change_api_credential(name, clear=True), 'demo': False}
            except (ValueError, OSError):
                raise HTTPException(500, f'Could not clear the private {label}; check /config permissions') from None

        @app.post(route + '/test', name=name + '_test')
        async def test_credential(request: Request):
            manager = request.app.state.manager
            if manager.demo:
                raise HTTPException(409, f'Demo mode never uses your live {label}')
            try:
                return await manager.api_credentials[name].test()
            except ValueError:
                raise HTTPException(409, f'Set a {label} in Settings (or the environment), then test it again') from None

    credential_routes('github', 'GitHub token')
    credential_routes('gemini', 'Gemini API key')

    @app.patch('/api/settings')
    async def save_settings(request:Request,data:dict=Body(...)):
        manager=request.app.state.manager
        try:
            updated=validate_settings(data,manager.settings())
        except ValueError as exc:
            # Pydantic messages can echo submitted secrets; only return fixed text.
            raise HTTPException(422,'Invalid settings: use valid timezone, HH:MM times, local model URL and complete soak tiers') from None
        was_included = manager.settings().include_stopped
        manager.store.save_settings(updated)
        if updated.include_stopped and not was_included:
            manager.request_poll()  # containers that were skipped need their first lookup
        return updated

    @app.post('/api/poll',status_code=202)
    async def poll(request:Request):
        manager=request.app.state.manager
        accepted=manager.request_poll()
        return {'status':'started' if accepted else ('demo' if manager.demo else 'already running')}

    @app.get('/api/poll/status')
    async def poll_status(request:Request):
        return request.app.state.manager.poll_status()

    @app.get('/api/logs')
    async def logs(request:Request):
        return request.app.state.manager.store.logs()

    @app.post('/api/demo')
    async def demo(request:Request):
        manager=request.app.state.manager
        try:
            manager.enter_demo()
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from None
        return {'demo':True}

    @app.delete('/api/demo')
    async def exit_demo(request:Request):
        manager=request.app.state.manager
        manager.leave_demo()
        manager.request_poll()
        return {'demo':False}

    @app.post('/api/notifications/test')
    async def test_notification(request:Request):
        manager=request.app.state.manager
        settings=manager.settings()
        if manager.demo:
            raise HTTPException(409,'Demo mode never sends household notifications')
        if not settings.notifications_enabled:
            raise HTTPException(409,'Enable notifications in Settings first')
        if is_quiet(settings):
            raise HTTPException(409,'Quiet hours are active; nothing was sent')
        if not manager.runtime.ha_token:
            raise HTTPException(409,'Configure your Home Assistant long-lived access token in Settings')
        if not manager.runtime.ha_url:
            raise HTTPException(409,'Configure your Home Assistant server URL in Settings')
        sent=await Notifier(manager.store,manager.runtime.ha_url,manager.runtime.ha_token).send(settings,'Test: Ripe is connected to your notification script. No update has been performed.','ripe-test')
        if not sent:
            raise HTTPException(502,'Household script call failed; see the source error log')
        return {'sent':True,'script':settings.notification_script}

    return app


app=create_app()
