from aiohttp.web import Application, middleware, Request, Response, json_response, HTTPFound, HTTPUnauthorized, HTTPClientError, HTTPException, HTTPBadRequest, HTTPNotFound, put, post, get, patch, delete
from typing import Any, Dict
from threading import Lock
from ..tests.helpers import createSnapshotTar, parseSnapshotInfo, all_addons
from ..logbase import LogBase
from ..time import Time
from ..helpers import formatException
from io import BytesIO
from injector import inject, singleton
from urllib.parse import quote
from oauth2client.client import OAuth2Credentials
import re
import random
import string
import asyncio
import logging
import aiohttp


mimeTypeQueryPattern = re.compile("^mimeType='.*'$")
parentsQueryPattern = re.compile("^'.*' in parents$")
bytesPattern = re.compile("^bytes \\d+-\\d+/\\d+$")
resumeBytesPattern = re.compile("^bytes \\*/\\d+$")
intPattern = re.compile("\\d+")
rangePattern = re.compile("bytes=\\d+-\\d+")


class HttpMultiException(HTTPClientError):
    def __init__(self, code):
        self.status_code = code
        self.statu = code


@singleton
class SimulationServer(LogBase):
    @inject
    def __init__(self, port, time: Time):
        self.items: Dict[str, Any] = {}
        self.upload_info: Dict[str, Any] = {}
        self.simulate_drive_errors = False
        self.simulate_out_of_drive_space = False
        self.error_code = 500
        self.match_errors = []
        self.last_error = False
        self.snapshots: Dict[str, Any] = {}
        self.snapshot_data: Dict[str, bytearray] = {}
        self.files: Dict[str, bytearray] = {}
        self.chunks = []
        self.settings: Dict[str, Any] = self.defaultSettings()
        self._snapshot_lock = asyncio.Lock()
        self._settings_lock = Lock()
        self._port = port
        self._ha_error = None
        self._entities = {}
        self._events = []
        self._attributes = {}
        self._notification = None
        self._time = time
        self._options = self.defaultOptions()
        self._username = "user"
        self._password = "pass"
        self.lostPermission = []
        self.urls = []
        self.relative = True
        self.block_snapshots = False
        self.snapshot_in_progress = False
        self.last_error_report = None

    def wasUrlRequested(self, pattern):
        for url in self.urls:
            if pattern in url:
                return True
        return False

    def blockSnapshots(self):
        self.block_snapshots = True

    def unBlockSnapshots(self):
        self.block_snapshots = False

    def setError(self, url_regx, attempts, status):
        self.match_errors.append({
            'url': url_regx,
            'attempts': attempts,
            'status': status
        })

    def defaultOptions(self):
        return {
            "max_snapshots_in_hassio": 4,
            "max_snapshots_in_google_drive": 4,
            "days_between_snapshots": 3,
            "use_ssl": False
        }

    def getEvents(self):
        return self._events.copy()

    def setHomeAssistantError(self, status_code):
        self._ha_error = status_code

    def getEntity(self, entity):
        return self._entities.get(entity)

    def getAttributes(self, attribute):
        return self._attributes.get(attribute)

    def getNotification(self):
        return self._notification

    def _reset(self) -> None:
        with self._settings_lock:
            self._ha_error = None
            self.items = {}
            self.upload_info = {}
            self.snapshots = {}
            self.snapshot_data = {}
            self.files = {}
            self._entities = {}
            self._attributes = {}
            self._notification = None
            self.settings = self.defaultSettings()
            self._options = self.defaultOptions()

    def getSetting(self, key):
        with self._settings_lock:
            return self.settings[key]

    def update(self, config):
        with self._settings_lock:
            self.settings.update(config)

    def defaultSettings(self):
        return {
            'snapshot_wait_time': 0,
            'snapshot_min_size': 1024 * 256 * 1,
            'snapshot_max_size': 1024 * 256 * 2,
            'ha_header': "test_header",
            "ha_version": "0.91.3",
            "ha_last_version": "0.91.2",
            "machine": "raspberrypi3",
            "ip_address": "172.30.32.1",
            "arch": "armv7",
            "image": "homeassistant/raspberrypi3-homeassistant",
            "custom": True,
            "drive_upload_error": None,
            "drive_upload_error_attempts": 0,
            "boot": True,
            "port": 8099,
            "ha_port": 1337,
            "ssl": False,
            "watchdog": True,
            "wait_boot": 600,
            "web_ui": "http://[HOST]:8099/",
            "ingress_url": "/index",
            "supervisor": "2.2.2",
            "homeassistant": "0.93.1",
            "hassos": "0.69.69",
            "hassio_error": None,
            "hassio_snapshot_error": None,
            "hostname": "localhost",
            "always_hard_lock": False,
            "supported_arch": [],
            "channel": "dev",
            "addon_slug": "self_slug",
            "drive_refresh_token": "",
            "drive_auth_token": "",
            "drive_client_id": "test_client_id",
            "drive_client_secret": "test_client_secret",
            "drive_upload_sleep": 0,
            "drive_all_error": None
        }

    def driveError(self) -> Any:
        if not self.simulate_drive_errors:
            return False
        if not self.last_error:
            self.last_error = True
            return self.error_code
        else:
            self.last_error = False
            return None

    async def readAll(self, request):
        data = bytearray()
        content = request.content
        while True:
            chunk, done = await content.readchunk()
            data.extend(chunk)
            if len(chunk) == 0:
                break
        return data

    def _checkDriveError(self, request: Request):
        if self.getSetting("drive_all_error"):
            raise HttpMultiException(self.getSetting("drive_all_error"))
        error = self.driveError()
        if error:
            raise HttpMultiException(error)
        for error in self.match_errors:
            if re.match(error['url'], str(request.url)):
                if error['attempts'] <= 0:
                    raise HttpMultiException(error['status'])
                else:
                    error['attempts'] = error['attempts'] - 1

    def _checkDriveHeaders(self, request: Request):
        self._checkDriveError(request)
        if request.headers.get("Authorization", "") != "Bearer " + self.getSetting('drive_auth_token'):
            raise HTTPUnauthorized()

    async def driveAuthentication(self, request: Request):
        self._checkDriveError(request)
        params = await request.post()
        if params['client_id'] != self.getSetting('drive_client_id'):
            raise HTTPUnauthorized()
        if params['client_secret'] != self.getSetting('drive_client_secret'):
            raise HTTPUnauthorized()
        if params['refresh_token'] != self.getSetting('drive_refresh_token'):
            raise HTTPUnauthorized()
        if params['grant_type'] != 'refresh_token':
            raise HTTPUnauthorized()

        new_token = self.generateId(20)
        with self._settings_lock:
            self.settings['drive_auth_token'] = new_token

        return json_response({
            'access_token': new_token,
            'expires_in': 3600,
            'token_type': 'who_cares'
        })

    async def reset(self, request: Request):
        self._reset()
        if isinstance(request, Request):
            self.update(request.query)
        if isinstance(request, Dict):
            self.update(request)

    async def uploadfile(self, request: Request):
        name: str = str(request.query.get("name", "test"))
        self.files[name] = await self.readAll(request)
        return Response(text="")

    async def readFile(self, request: Request):
        return self.serve_bytes(request, self.files[request.query.get("name", "test")])

    def serve_bytes(self, request: Request, bytes: bytearray, include_length: bool = True) -> Any:
        if "Range" in request.headers:
            # Do range request
            if not rangePattern.match(request.headers['Range']):
                raise HTTPBadRequest()

            numbers = intPattern.findall(request.headers['Range'])
            start = int(numbers[0])
            end = int(numbers[1])

            if start < 0:
                raise HTTPBadRequest()
            if start > end:
                raise HTTPBadRequest()
            if end > len(bytes) - 1:
                raise HTTPBadRequest()
            resp = Response(body=bytes[start:end + 1], status=206)
            resp.headers['Content-Range'] = "bytes {0}-{1}/{2}".format(
                start, end, len(bytes))
            if include_length:
                resp.headers["Content-length"] = str(len(bytes))
            return resp
        else:
            resp = Response(body=bytes)
            resp.headers["Content-length"] = str(len(bytes))
            return resp

    async def updateSettings(self, request: Request):
        data = await request.json()
        with self._settings_lock:
            for key in data:
                self.settings[key] = data[key]
        return Response(text="updated")

    async def driveGetItem(self, request: Request):
        id = request.match_info.get('id')
        self._checkDriveHeaders(request)
        if id not in self.items:
            raise HTTPNotFound
        request_type = request.query.get("alt", "metadata")
        if request_type == "media":
            # return bytes
            item = self.items[id]
            if 'bytes' not in item:
                raise HTTPBadRequest
            return self.serve_bytes(request, item['bytes'], include_length=False)
        else:
            fields = request.query.get("fields", "id").split(",")
            return json_response(self.filter_fields(self.items[id], fields))

    async def driveUpdate(self, request: Request):
        id = request.match_info.get('id')
        self._checkDriveHeaders(request)
        if id not in self.items:
            return HTTPNotFound
        update = await request.json()
        for key in update:
            if key in self.items[id] and isinstance(self.items[id][key], dict):
                self.items[id][key].update(update[key])
            else:
                self.items[id][key] = update[key]
        return Response()

    async def driveDelete(self, request: Request):
        id = request.match_info.get('id')
        self._checkDriveHeaders(request)
        if id not in self.items:
            raise HTTPNotFound
        del self.items[id]
        return Response()

    async def driveQuery(self, request: Request):
        self._checkDriveHeaders(request)
        query: str = request.query.get("q", "")
        fields = self.parseFields(request.query.get('fields', 'id'))
        if mimeTypeQueryPattern.match(query):
            ret = []
            mimeType = query[len("mimeType='"):-1]
            for item in self.items.values():
                if item.get('mimeType', '') == mimeType:
                    ret.append(self.filter_fields(item, fields))
            return json_response({'files': ret})
        elif parentsQueryPattern.match(query):
            ret = []
            parent = query[1:-len("' in parents")]
            if parent not in self.items:
                raise HTTPNotFound
            if parent in self.lostPermission:
                return Response(
                    status=403,
                    content_type="application/json",
                    text='{"error": {"errors": [{"reason": "forbidden"}]}}')
            for item in self.items.values():
                if parent in item.get('parents', []):
                    ret.append(self.filter_fields(item, fields))
            return json_response({'files': ret})
        elif len(query) == 0:
            ret = []
            for item in self.items.values():
                ret.append(self.filter_fields(item, fields))
            return json_response({'files': ret})
        else:
            raise HTTPBadRequest

    async def driveCreate(self, request: Request):
        self._checkDriveHeaders(request)
        id = self.generateId(30)
        item = self.formatItem(await request.json(), id)
        self.items[id] = item
        return json_response({'id': item['id']})

    async def driveStartUpload(self, request: Request):
        if self.simulate_out_of_drive_space:
            return json_response({
                "error": {
                    "errors": [
                        {"reason": "storageQuotaExceeded"}
                    ]
                }
            }, status=400)
        logging.getLogger().info("Drive start upload request")
        self._checkDriveHeaders(request)
        if request.query.get('uploadType') != 'resumable':
            raise HTTPBadRequest()
        mimeType = request.headers.get('X-Upload-Content-Type', None)
        if mimeType is None:
            raise HTTPBadRequest()
        size = int(request.headers.get('X-Upload-Content-Length', -1))
        if size == -1:
            raise HTTPBadRequest()
        metadata = await request.json()
        id = self.generateId()

        # Validate parents
        if 'parents' in metadata:
            for parent in metadata['parents']:
                if parent not in self.items:
                    raise HTTPNotFound()
                if parent in self.lostPermission:
                    return Response(status=403, content_type="application/json", text='{"error": {"errors": [{"reason": "forbidden"}]}}')
        self.upload_info['size'] = size
        self.upload_info['mime'] = mimeType
        self.upload_info['item'] = self.formatItem(metadata, id)
        self.upload_info['id'] = id
        self.upload_info['next_start'] = 0
        metadata['bytes'] = bytearray()
        metadata['size'] = size
        resp = Response()
        resp.headers['Location'] = "http://localhost:" + \
            str(self._port) + "/upload/drive/v3/files/progress/" + id
        return resp

    async def driveContinueUpload(self, request: Request):
        id = request.match_info.get('id')
        if (self.getSetting('drive_upload_sleep') > 0):
            await self._time.sleepAsync(self.getSetting('drive_upload_sleep'))
        self._checkDriveHeaders(request)
        if self.upload_info.get('id', "") != id:
            raise HTTPBadRequest()
        chunk_size = int(request.headers['Content-Length'])
        info = request.headers['Content-Range']
        if resumeBytesPattern.match(info):
            resp = Response(status=308)
            if self.upload_info['next_start'] != 0:
                resp.headers['Range'] = "bytes=0-{0}".format(self.upload_info['next_start'] - 1)
            return resp
        if not bytesPattern.match(info):
            raise HTTPBadRequest()
        numbers = intPattern.findall(info)
        start = int(numbers[0])
        end = int(numbers[1])
        total = int(numbers[2])
        if total != self.upload_info['size']:
            raise HTTPBadRequest()
        if start != self.upload_info['next_start']:
            raise HTTPBadRequest()
        if not (end == total - 1 or chunk_size % (256 * 1024) == 0):
            raise HTTPBadRequest()
        if end > total - 1:
            raise HTTPBadRequest()

        # get the chunk
        received_bytes = await self.readAll(request)

        # See if we shoudl fail the request
        if self.getSetting("drive_upload_error") is not None:
            if self.getSetting("drive_upload_error_attempts") <= 0:
                raise HttpMultiException(self.getSetting("drive_upload_error"))
            else:
                self.update({"drive_upload_error_attempts": self.getSetting("drive_upload_error_attempts") - 1})

        # validate the chunk
        if len(received_bytes) != chunk_size:
            raise HTTPBadRequest()

        if len(received_bytes) != end - start + 1:
            raise HTTPBadRequest()

        self.upload_info['item']['bytes'].extend(received_bytes)

        if len(self.upload_info['item']['bytes']) != end + 1:
            raise HTTPBadRequest()

        self.chunks.append(len(received_bytes))
        if end == total - 1:
            # upload is complete, so create the item
            self.items[self.upload_info['id']] = self.upload_info['item']
            return json_response({"id": self.upload_info['id']})
        else:
            # Return an incomplete response
            # TODO: For some reason, the tests like to stop right here
            resp = Response(status=308)
            self.upload_info['next_start'] = end + 1
            resp.headers['Range'] = "bytes=0-{0}".format(end)
            return resp

    # HASSIO METHODS BELOW
    def _verifyHassioHeader(self, request) -> bool:
        if self.getSetting("hassio_error") is not None:
            raise HttpMultiException(self.getSetting("hassio_error"))
        self._verifyHeader(request, "X-HASSIO-KEY",
                           self.getSetting('ha_header'))

    def _verifyHaHeader(self, request) -> bool:
        if self._ha_error is not None:
            raise HttpMultiException(self._ha_error)
        self._verifyHeader(request, "Authorization", "Bearer " + self.getSetting('ha_header'))

    def _verifyHeader(self, request, key: str, value: str) -> bool:
        if request.headers.get(key, None) != value:
            raise HTTPUnauthorized()

    def formatDataResponse(self, data: Any) -> str:
        return json_response({'result': 'ok', 'data': data})

    def formatErrorResponse(self, error: str) -> str:
        return json_response({'result': error})

    async def hassioSnapshots(self, request: Request):
        self._verifyHassioHeader(request)
        return self.formatDataResponse({'snapshots': list(self.snapshots.values())})

    async def hassioSupervisorInfo(self, request: Request):
        self._verifyHassioHeader(request)
        return self.formatDataResponse(
            {
                "addons": list(all_addons).copy()
            }
        )

    async def haInfo(self, request: Request):
        self._verifyHassioHeader(request)
        return self.formatDataResponse(
            {
                "version": self.getSetting('ha_version'),
                "last_version": self.getSetting('ha_last_version'),
                "machine": self.getSetting('machine'),
                "ip_address": self.getSetting('ip_address'),
                "arch": self.getSetting('arch'),
                "image": self.getSetting('image'),
                "custom": self.getSetting('custom'),
                "boot": self.getSetting('boot'),
                "port": self.getSetting('ha_port'),
                "ssl": self.getSetting('ssl'),
                "watchdog": self.getSetting('watchdog'),
                "wait_boot": self.getSetting('wait_boot')
            }
        )

    async def hassioNewFullSnapshot(self, request: Request):
        if (self.block_snapshots or self.snapshot_in_progress) and not self.getSetting('always_hard_lock'):
            raise HTTPBadRequest()

        input_json = await request.json()
        try:
            await self._snapshot_lock.acquire()
            self.snapshot_in_progress = True
            self._verifyHassioHeader(request)
            error = self.getSetting("hassio_snapshot_error")
            if error is not None:
                raise HttpMultiException(error)

            seconds = int(request.query.get(
                'seconds', self.getSetting('snapshot_wait_time')))
            date = self._time.now()
            size = int(random.uniform(float(self.getSetting('snapshot_min_size')), float(
                self.getSetting('snapshot_max_size'))))
            slug = self.generateId(8)
            name = input_json['name']
            password = input_json.get('password', None)
            if seconds > 0:
                await asyncio.sleep(seconds)

            data = createSnapshotTar(slug, name, date, size, password=password)
            snapshot_info = parseSnapshotInfo(data)
            self.snapshots[slug] = snapshot_info
            self.snapshot_data[slug] = bytearray(data.getbuffer())
            return self.formatDataResponse({"slug": slug})
        finally:
            self.snapshot_in_progress = False
            self._snapshot_lock.release()

    async def hassioNewPartialSnapshot(self, request: Request):
        if (self.block_snapshots or self.snapshot_in_progress) and not self.getSetting('always_hard_lock'):
            raise HTTPBadRequest()
        input_json = await request.json()
        try:
            await self._snapshot_lock.acquire()
            self.snapshot_in_progress = True
            self._verifyHassioHeader(request)
            seconds = int(request.query.get(
                'seconds', self.getSetting('snapshot_wait_time')))
            date = self._time.now()
            size = int(random.uniform(float(self.getSetting('snapshot_min_size')), float(
                self.getSetting('snapshot_max_size'))))
            slug = self.generateId(8)
            name = input_json['name']
            password = input_json.get('password', None)
            if seconds > 0:
                await asyncio.sleep(seconds)

            data = createSnapshotTar(
                slug,
                name,
                date,
                size,
                included_folders=input_json['folders'],
                included_addons=input_json['addons'],
                password=password)
            snapshot_info = parseSnapshotInfo(data)
            self.snapshots[slug] = snapshot_info
            self.snapshot_data[slug] = bytearray(data.getbuffer())
            return self.formatDataResponse({"slug": slug})
        finally:
            self.snapshot_in_progress = False
            self._snapshot_lock.release()

    async def uploadNewSnapshot(self, request: Request):
        self._verifyHassioHeader(request)
        try:
            received_bytes = await self.readAll(request)
            info = parseSnapshotInfo(BytesIO(received_bytes))
            self.snapshots[info['slug']] = info
            self.snapshot_data[info['slug']] = received_bytes
            return self.formatDataResponse({"slug": info['slug']})
        except Exception as e:
            print(str(e))
            return self.formatErrorResponse("Bad snapshot")

    async def hassioDelete(self, request: Request):
        slug = request.match_info.get('slug')
        self._verifyHassioHeader(request)
        if slug not in self.snapshots:
            raise HTTPNotFound()
        del self.snapshots[slug]
        del self.snapshot_data[slug]
        return self.formatDataResponse("deleted")

    async def hassioSnapshotInfo(self, request: Request):
        slug = request.match_info.get('slug')
        self._verifyHassioHeader(request)
        if slug not in self.snapshots:
            raise HTTPNotFound()
        return self.formatDataResponse(self.snapshots[slug])

    async def hassioSnapshotDownload(self, request: Request):
        slug = request.match_info.get('slug')
        self._verifyHassioHeader(request)
        if slug not in self.snapshot_data:
            raise HTTPNotFound()
        return self.serve_bytes(request, self.snapshot_data[slug])

    async def hassioSelfInfo(self, request: Request):
        self._verifyHassioHeader(request)
        return self.formatDataResponse({
            "webui": self.getSetting('web_ui'),
            'ingress_url': self.getSetting('ingress_url'),
            "slug": self.getSetting('addon_slug'),
            "options": self._options
        })

    async def hassioInfo(self, request: Request):
        self._verifyHassioHeader(request)
        return self.formatDataResponse({
            "supervisor": self.getSetting('supervisor'),
            "homeassistant": self.getSetting('homeassistant'),
            "hassos": self.getSetting('hassos'),
            "hostname": self.getSetting('hostname'),
            "machine": self.getSetting('machine'),
            "arch": self.getSetting('arch'),
            "supported_arch": self.getSetting('supported_arch'),
            "channel": self.getSetting('channel')
        })

    async def hassioAuthenticate(self, request: Request):
        self._verifyHassioHeader(request)
        input_json = await request.json()
        if input_json.get("username") != self._username or input_json.get("password") != self._password:
            raise HTTPBadRequest()
        return self.formatDataResponse({})

    async def haStateUpdate(self, request: Request):
        entity = request.match_info.get('entity')
        self._verifyHaHeader(request)
        json = await request.json()
        self._entities[entity] = json['state']
        self._attributes[entity] = json['attributes']
        return Response()

    async def haEventUpdate(self, request: Request):
        name = request.match_info.get('name')
        self._verifyHaHeader(request)
        self._events.append((name, await request.json()))
        return Response()

    async def createNotification(self, request: Request):
        self._verifyHaHeader(request)
        notification = await request.json()
        print("Created notification with: {}".format(notification))
        self._notification = notification.copy()
        return Response()

    async def dismissNotification(self, request: Request):
        self._verifyHaHeader(request)
        print("Dismissed notification with: {}".format(await request.json()))
        self._notification = None
        return Response()

    async def hassioUpdateOptions(self, request: Request):
        self._verifyHassioHeader(request)
        self._options = (await request.json())['options'].copy()
        return self.formatDataResponse({})

    async def driveCredGenerate(self, request: Request):
        # build valid credentials
        creds = OAuth2Credentials(
            "",
            self.getSetting("drive_client_id"),
            self.getSetting("drive_client_secret"),
            refresh_token=self.getSetting("drive_refresh_token"),
            token_expiry="",
            token_uri="",
            user_agent="")
        url = request.query["redirectbacktoken"] + "?creds=" + quote(creds.to_json())
        raise HTTPFound(location=url)

    async def errorReport(self, request: Request):
        self.last_error_report = request.query["error"]
        return Response()

    @middleware
    async def error_middleware(self, request: Request, handler):
        self.urls.append(str(request.url))
        try:
            resp = await handler(request)
            return resp
        except Exception as ex:
            await self.readAll(request)
            if isinstance(ex, HttpMultiException):
                return Response(status=ex.status_code)
            elif isinstance(ex, HTTPException):
                raise
            else:
                self.error(formatException(ex))
            return json_response(str(ex), status=502)

    def createApp(self):
        app = Application(middlewares=[self.error_middleware])
        app.add_routes(self.routes())
        return app

    def routes(self):
        return [
            get('/external/drivecreds/', self.driveCredGenerate),
            post('/external/drivecreds/', self.driveCredGenerate),
            post('/addons/self/options', self.hassioUpdateOptions),
            post("/homeassistant/api/services/persistent_notification/dismiss", self.dismissNotification),
            post("/homeassistant/api/services/persistent_notification/create", self.createNotification),
            post("/homeassistant/api/events/{name}", self.haEventUpdate),
            post("/homeassistant/api/states/{entity}", self.haStateUpdate),
            post('/auth', self.hassioAuthenticate),
            get('/auth', self.hassioAuthenticate),
            get('/info', self.hassioInfo),
            get('/addons/self/info', self.hassioSelfInfo),
            get('/snapshots/{slug}/download', self.hassioSnapshotDownload),
            get('/snapshots/{slug}/info', self.hassioSnapshotInfo),
            post('/snapshots/{slug}/remove', self.hassioDelete),
            post('/snapshots/new/upload', self.uploadNewSnapshot),
            get('/snapshots/new/upload', self.uploadNewSnapshot),
            post('/snapshots/new/partial', self.hassioNewPartialSnapshot),
            post('/snapshots/new/full', self.hassioNewFullSnapshot),
            get('/homeassistant/info', self.haInfo),
            get('/supervisor/info', self.hassioSupervisorInfo),
            get('/snapshots', self.hassioSnapshots),
            put('/upload/drive/v3/files/progress/{id}', self.driveContinueUpload),
            post('/upload/drive/v3/files/', self.driveStartUpload),
            post('/drive/v3/files/', self.driveCreate),
            get('/drive/v3/files/', self.driveQuery),
            delete('/drive/v3/files/{id}/', self.driveDelete),
            patch('/drive/v3/files/{id}/', self.driveUpdate),
            get('/drive/v3/files/{id}/', self.driveGetItem),
            post('/updatesettings', self.updateSettings),
            get('/readfile', self.readFile),
            post('/uploadfile', self.uploadfile),
            post('/doareset', self.reset),
            post('/oauth2/v4/token', self.driveAuthentication),
            get('/errorreport', self.errorReport)
        ]

    def generateId(self, length: int = 30) -> Any:
        return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(length))

    def timeToRfc3339String(self, time) -> Any:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ")

    def formatItem(self, base, id):
        base['capabilities'] = {'canAddChildren': True,
                                'canListChildren': True, 'canDeleteChildren': True}
        base['trashed'] = False
        base['id'] = id
        base['modifiedTime'] = self.timeToRfc3339String(self._time.now())
        return base

    def parseFields(self, source: str):
        fields = []
        for field in source.split(","):
            if field.startswith("files("):
                fields.append(field[6:])
            elif field.endswith(")"):
                fields.append(field[:-1])
            else:
                fields.append(field)
        return fields

    def filter_fields(self, item: Dict[str, Any], fields) -> Dict[str, Any]:
        ret = {}
        for field in fields:
            if field in item:
                ret[field] = item[field]
        return ret


async def main():
    server = SimulationServer(56154, Time())
    await server.reset({
        'snapshot_min_size': 1024 * 1024 * 3,
        'snapshot_max_size': 1024 * 1024 * 5,
        "drive_refresh_token": "test_refresh_token",
        "drive_client_id": "test_client_id",
        "drive_client_secret": "test_client_secret",
        "drive_upload_sleep": 5,
        "snapshot_wait_time": 15,
        "hassio_header": "test_header"
    })

    # start the server
    runner = aiohttp.web.AppRunner(server.createApp())
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", port=56154)
    await site.start()
    print("Server started")

    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await runner.shutdown()
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
