"""Versioned client/gateway RPC boundary and per-message authorization."""

import copy

CONTRACT_VERSION = 1
API_VERSION = 1
RPC_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024
MAX_WATCH_THREADS = 32
MAX_THREAD_ID_LENGTH = 256

_METHODS = {
    'thread.watch': {'access': 'read', 'description': 'Subscribe to rollout detail for thread IDs.'},
    'thread.unwatch': {'access': 'read', 'description': 'Stop a rollout detail subscription.'},
    'thread.setWatches': {'access': 'read', 'description': 'Replace rollout detail subscriptions.'},
}
_LEGACY_METHODS = {'watch': 'thread.watch', 'unwatch': 'thread.unwatch',
                   'set_watch': 'thread.setWatches'}


def contract_document():
    return {
        'contractVersion': CONTRACT_VERSION,
        'http': {
            'apiVersion': API_VERSION,
            'versionHeader': 'apiContractVersion',
            'authentication': {'health': 'public-metadata', 'read': ['full', 'view'],
                               'write': ['full'],
                               'viewRestrictedPaths': ['/api/fs/*', '/api/approvals']},
        },
        'websocket': {
            'path': '/api/ws',
            'versionQuery': 'rpc',
            'version': RPC_VERSION,
            'authentication': 'revalidate-token-for-every-client-message',
            'defaultMethodPolicy': 'deny',
            'maxFrameBytes': MAX_FRAME_BYTES,
            'maxWatchThreads': MAX_WATCH_THREADS,
            'envelope': {'version': RPC_VERSION, 'id': 'optional request identifier',
                         'method': 'string', 'params': {'threadIds': 'string[]'}},
            'legacyEnvelope': {'type': 'watch|unwatch|set_watch', 'tids': 'string[]'},
            'methods': copy.deepcopy(_METHODS),
        },
    }


def negotiate_ws_version(value=None):
    """Missing query versions mean v1 for older AN clients; other versions fail closed."""
    if value in (None, ''):
        return RPC_VERSION, None
    try:
        version = int(value)
    except (TypeError, ValueError):
        return None, {'code': 'rpc_version_unsupported',
                      'message': 'WebSocket RPC version must be an integer',
                      'supportedVersion': RPC_VERSION}
    if version != RPC_VERSION:
        return None, {'code': 'rpc_version_unsupported',
                      'message': 'Unsupported WebSocket RPC version',
                      'supportedVersion': RPC_VERSION}
    return version, None


def authorize_http(method, path, role):
    """Shared coarse REST authorization policy; endpoint-specific guards stay in place."""
    method = str(method or '').upper()
    if method == 'GET' and path == '/api/health':
        return None
    if role not in ('full', 'view'):
        return {'code': 'unauthorized', 'status': 401}
    if method == 'GET':
        return None
    if role == 'view':
        return {'code': 'read_only', 'status': 403}
    if method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        return None
    return {'code': 'method_not_allowed', 'status': 405}


def authorize_message(message, role, authenticated=True):
    """Validate one WS call. The caller must re-check the presented token first."""
    if not authenticated or role not in ('full', 'view'):
        return None, {'code': 'unauthorized', 'message': 'WebSocket authorization expired'}
    if not isinstance(message, dict):
        return None, {'code': 'invalid_envelope', 'message': 'RPC message must be an object'}

    request_id = message.get('id')
    if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 128):
        return None, {'code': 'invalid_envelope', 'message': 'id must be a string of at most 128 characters'}
    version = message.get('version')
    if version is not None:
        if version != RPC_VERSION:
            return None, {'id': request_id, 'code': 'version_unsupported',
                          'message': 'Unsupported WebSocket RPC version'}
        method = message.get('method')
        params = message.get('params') or {}
    else:
        method = _LEGACY_METHODS.get(message.get('type'))
        params = {'threadIds': message.get('tids') or []}

    spec = _METHODS.get(method)
    if spec is None:
        return None, {'id': request_id, 'code': 'method_not_allowed',
                      'message': 'RPC method is not registered'}
    if spec['access'] == 'write' and role != 'full':
        return None, {'id': request_id, 'code': 'forbidden',
                      'message': 'This RPC method requires full access'}
    if not isinstance(params, dict):
        return None, {'id': request_id, 'code': 'invalid_params', 'message': 'params must be an object'}

    thread_ids = params.get('threadIds')
    if not isinstance(thread_ids, list) or len(thread_ids) > MAX_WATCH_THREADS:
        return None, {'id': request_id, 'code': 'invalid_params',
                      'message': 'threadIds must be an array of at most 32 entries'}
    normalized = []
    for thread_id in thread_ids:
        if not isinstance(thread_id, str) or not thread_id or len(thread_id) > MAX_THREAD_ID_LENGTH:
            return None, {'id': request_id, 'code': 'invalid_params', 'message': 'Invalid thread ID'}
        if thread_id not in normalized:
            normalized.append(thread_id)
    return {'id': request_id, 'method': method, 'threadIds': normalized}, None
