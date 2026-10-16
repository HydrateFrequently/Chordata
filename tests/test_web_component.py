"""Moonraker component contracts and provider failure/privacy regressions."""
import copy
from enum import Flag, auto
import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock

# Mirror only Moonraker's flags; HTTPRequest and its validation are real Tornado.
class RequestType(Flag):
    GET = auto()
    POST = auto()
    DELETE = auto()

class TransportType(Flag):
    HTTP = auto()
    WEBSOCKET = auto()
    MQTT = auto()
    INTERNAL = auto()

common = ModuleType('_chordata_test.common')
common.RequestType, common.TransportType = RequestType, TransportType
sys.modules[common.__name__] = common
spec = importlib.util.spec_from_file_location('_chordata_test.components.chordata_web', Path(__file__).resolve().parents[1] / 'moonraker/chordata_web.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


def status():
    belt = dict(freq=110., used=5, heard=5, plucks=[dict(ok=True, freq=110.) for _ in range(5)])
    return dict(state='idle', belts=dict(target=110., tol=.25, plucks=5, round=0, heard=[], A=copy.deepcopy(belt), B=copy.deepcopy(belt)))


def reply(choice='compare', confidence=.9):
    return {'answers': {'review': dict(type='choice', choice=choice, confidence=confidence,
        probabilities={key: .9 if key == choice else 1/30 for key in web.MESSAGES})}}


class Server:
    def __init__(self, root):
        self.root, self.routes, self.warnings = root, {}, []
        self.klippy = SimpleNamespace(query_objects=AsyncMock(return_value={'chordata': status()}))
        self.files = SimpleNamespace(add_reserved_path=lambda *args, **kwargs: None)
    def get_app_args(self): return {'data_path': self.root}
    def register_endpoint(self, endpoint, request_types, callback, transports=None, wrap_result=True, content_type=None, auth_required=True):
        self.routes[endpoint] = dict(callback=callback, auth_required=auth_required, transports=transports)
    def add_warning(self, message): self.warnings.append(message)
    def lookup_component(self, name): return self.klippy if name == 'klippy_apis' else self.files
    def error(self, message, code=500): return ValueError(message)


class ComponentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = Server(self.temp.name)
        config = SimpleNamespace(get_server=lambda: self.server, get=lambda key, default=None: default)
        self.component = web.ChordataWeb(config)
        self.component.http.close()
        self.component.http = SimpleNamespace(fetch=AsyncMock(return_value=SimpleNamespace(body=json.dumps(reply()).encode())), close=lambda: None)
    async def asyncTearDown(self):
        await self.component.close()
        self.temp.cleanup()
    def enable(self):
        self.component.settings = dict(web.DEFAULTS, enabled=True, endpoint='https://inference.example/predict', api_key='private-secret')
    async def evaluate(self): return await self.component._evaluate(SimpleNamespace())
    async def test_disabled_never_queries_printer_or_provider(self):
        self.assertEqual((await self.evaluate())['status'], 'disabled')
        self.component.http.fetch.assert_not_awaited()
        self.server.klippy.query_objects.assert_not_awaited()
    async def test_routes_protect_settings_and_evaluation(self):
        protected = [route for name, route in self.server.routes.items() if name != '/server/chordata']
        self.assertEqual(len(protected), 2)
        self.assertTrue(all(route['auth_required'] for route in protected))
        self.assertTrue(any(name.startswith('/access/') for name in self.server.routes))
    async def test_configuration_is_private_and_never_returns_key(self):
        request = SimpleNamespace(get_args=lambda: dict(action='configure', enabled=True, endpoint='https://inference.example/predict', api_key='private-secret'))
        public = await self.component._handle_ai(request)
        self.assertNotIn('api_key', public)
        self.assertTrue(public['has_api_key'])
        self.assertEqual(stat.S_IMODE(self.component.settings_path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.component.settings_path.read_text())['api_key'], 'private-secret')
    async def test_valid_review_request_has_bounded_data_and_no_redirect(self):
        self.enable()
        self.assertEqual((await self.evaluate())['status'], 'ok')
        request = self.component.http.fetch.await_args.args[0]
        self.assertFalse(request.follow_redirects)
        self.assertTrue(request.validate_cert)
        self.assertEqual(request.headers['Authorization'], 'Bearer private-secret')
        payload = json.loads(request.body)
        self.assertNotIn('private-secret', json.dumps(payload))
        self.assertNotIn('plucks', payload['state']['belts']['A'])
    async def test_malformed_provider_responses_fail_closed(self):
        self.enable()
        for payload in [None, [], {}, {'answers': []}, reply(confidence=float('nan')), reply(choice='tighten')]:
            with self.subTest(payload=payload):
                self.component.last_request = -float('inf')
                self.component.http.fetch.return_value.body = json.dumps(payload).encode()
                result = await self.evaluate()
                self.assertEqual(result['status'], 'unavailable')
                self.assertNotIn('private-secret', json.dumps(result))
    async def test_changed_measurement_discards_review(self):
        self.enable()
        changed = status()
        changed['belts']['A']['freq'] = 111.
        self.server.klippy.query_objects.side_effect = [{'chordata': status()}, {'chordata': changed}]
        self.assertEqual((await self.evaluate())['status'], 'abstained')
    async def test_identical_frequency_new_capture_discards_review(self):
        self.enable()
        first, second = status(), status()
        first['belts']['A']['measured_at'] = 1000.
        second['belts']['A']['measured_at'] = 1001.
        self.server.klippy.query_objects.side_effect = [{'chordata': first}, {'chordata': second}]
        self.assertEqual((await self.evaluate())['status'], 'abstained')
    async def test_changed_settings_discards_review(self):
        self.enable()
        async def fetch(request):
            self.component.settings['enabled'] = False
            return SimpleNamespace(body=json.dumps(reply()).encode())
        self.component.http.fetch.side_effect = fetch
        self.assertEqual((await self.evaluate())['status'], 'abstained')
    async def test_errors_do_not_expose_provider_details(self):
        self.enable()
        self.component.http.fetch.side_effect = RuntimeError('private-secret https://private-host')
        result = await self.evaluate()
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('private-secret', json.dumps(result))
        self.assertNotIn('private-host', json.dumps(result))
    async def test_recording_rejected_before_provider(self):
        self.enable()
        self.server.klippy.query_objects.return_value = {'chordata': dict(state='recording')}
        self.assertEqual((await self.evaluate())['status'], 'unavailable')
        self.component.http.fetch.assert_not_awaited()


class SettingsTests(unittest.TestCase):
    def test_endpoint_or_provider_changes_clear_old_credentials(self):
        old = dict(web.DEFAULTS, endpoint='https://old.example/predict', api_key='old-secret')
        self.assertEqual(web.validate_settings({'endpoint': 'https://new.example/predict'}, old)['api_key'], '')
        self.assertEqual(web.validate_settings({'provider': 'jev'}, old)['api_key'], '')
        self.assertEqual(web.validate_settings({'model': 'another'}, old)['api_key'], 'old-secret')
    def test_invalid_endpoints_rejected(self):
        for endpoint in ['file:///etc/passwd', 'https://user:secret@host/predict', 'https://host/predict?secret=x', 'https://host:bad/predict', 'http://host/\npath']:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                web.validate_settings({'endpoint': endpoint})
    def test_partial_pair_cannot_receive_compare_advice(self):
        sample = status()
        sample['belts']['heard'] = ['A']
        self.assertEqual(web.decode_review(reply(), .7, web.measurement_summary(sample))['status'], 'abstained')
    def test_low_confidence_abstains(self):
        self.assertEqual(web.decode_review(reply(confidence=.4), .7, web.measurement_summary(status()))['status'], 'abstained')

if __name__ == '__main__':
    unittest.main()
