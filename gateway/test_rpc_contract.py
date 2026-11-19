import unittest

from rpc_contract import (API_VERSION, CONTRACT_VERSION, RPC_VERSION, authorize_http, authorize_message,
                          contract_document, negotiate_ws_version)


class RpcContractTests(unittest.TestCase):
    def test_contract_publishes_versions_and_deny_by_default(self):
        contract = contract_document()
        self.assertEqual(contract['contractVersion'], CONTRACT_VERSION)
        self.assertEqual(contract['http']['apiVersion'], API_VERSION)
        self.assertEqual(contract['websocket']['version'], RPC_VERSION)
        self.assertEqual(contract['websocket']['defaultMethodPolicy'], 'deny')
        self.assertEqual(contract['websocket']['authentication'],
                         'revalidate-token-for-every-client-message')
        self.assertEqual(set(contract['websocket']['methods']), {
            'thread.watch', 'thread.unwatch', 'thread.setWatches'})

    def test_versioned_read_method_authorizes_each_message(self):
        request, error = authorize_message(
            {'version': RPC_VERSION, 'id': 'r-1', 'method': 'thread.setWatches',
             'params': {'threadIds': ['a', 'a', 'b']}}, 'view')
        self.assertIsNone(error)
        self.assertEqual(request['threadIds'], ['a', 'b'])

    def test_authentication_expiry_rejects_existing_socket_method_call(self):
        request, error = authorize_message(
            {'version': RPC_VERSION, 'id': 'r-2', 'method': 'thread.setWatches',
             'params': {'threadIds': ['a']}}, None, authenticated=False)
        self.assertIsNone(request)
        self.assertEqual(error['code'], 'unauthorized')

    def test_unknown_and_unregistered_write_methods_are_denied(self):
        for role in ('view', 'full'):
            request, error = authorize_message(
                {'version': RPC_VERSION, 'id': 'r-3', 'method': 'thread.delete',
                 'params': {'threadIds': ['a']}}, role)
            self.assertIsNone(request)
            self.assertEqual(error['code'], 'method_not_allowed')

    def test_legacy_watch_envelope_remains_read_only(self):
        request, error = authorize_message({'type': 'set_watch', 'tids': ['thread-1']}, 'view')
        self.assertIsNone(error)
        self.assertEqual(request['method'], 'thread.setWatches')
        self.assertEqual(request['threadIds'], ['thread-1'])
        self.assertEqual(authorize_message({'type': 'delete_thread', 'tids': ['thread-1']}, 'full')[1]['code'],
                         'method_not_allowed')

    def test_version_and_payload_limits_are_enforced(self):
        _, version_error = authorize_message(
            {'version': RPC_VERSION + 1, 'method': 'thread.watch', 'params': {'threadIds': []}}, 'full')
        self.assertEqual(version_error['code'], 'version_unsupported')
        _, limit_error = authorize_message(
            {'version': RPC_VERSION, 'method': 'thread.watch',
             'params': {'threadIds': [str(i) for i in range(33)]}}, 'full')
        self.assertEqual(limit_error['code'], 'invalid_params')

    def test_handshake_negotiates_independent_websocket_version(self):
        self.assertEqual(negotiate_ws_version(None), (RPC_VERSION, None))
        self.assertEqual(negotiate_ws_version(str(RPC_VERSION)), (RPC_VERSION, None))
        version, error = negotiate_ws_version(str(RPC_VERSION + 1))
        self.assertIsNone(version)
        self.assertEqual(error['code'], 'rpc_version_unsupported')

    def test_http_role_policy_is_centralized(self):
        self.assertIsNone(authorize_http('GET', '/api/rpc/contract', 'view'))
        self.assertEqual(authorize_http('POST', '/api/threads/t/stop', 'view')['code'], 'read_only')
        self.assertEqual(authorize_http('GET', '/api/threads', None)['code'], 'unauthorized')
        self.assertIsNone(authorize_http('POST', '/api/threads/t/stop', 'full'))
        self.assertEqual(authorize_http('TRACE', '/api/threads', 'full')['status'], 405)
        version, error = negotiate_ws_version('invalid')
        self.assertIsNone(version)
        self.assertEqual(error['code'], 'rpc_version_unsupported')


if __name__ == '__main__':
    unittest.main()
