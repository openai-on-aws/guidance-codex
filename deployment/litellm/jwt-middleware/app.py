"""
JWT Middleware for LiteLLM Gateway

Validates JWT tokens from corporate IdP (Okta, Azure AD, etc.)
and automatically manages LiteLLM API keys for authenticated users.

This allows self-service API key generation via SSO without requiring
LiteLLM Enterprise license.
"""
import os
import json
import time
import logging
from functools import wraps
from typing import Dict, Optional

import jwt
import requests
import boto3
from flask import Flask, request, jsonify, Response
from cachetools import TTLCache
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration from environment variables
JWKS_URL = os.environ.get('JWKS_URL')
JWT_AUDIENCE = os.environ.get('JWT_AUDIENCE')
JWT_ISSUER = os.environ.get('JWT_ISSUER')
LITELLM_URL = os.environ.get('LITELLM_URL', 'http://localhost:4000')
LITELLM_MASTER_KEY = os.environ.get('LITELLM_MASTER_KEY')
DYNAMODB_TABLE = os.environ.get('DYNAMODB_TABLE', 'codex-user-keys')
AWS_REGION = os.environ.get('AWS_REGION', 'us-west-2')

# Validate required configuration
required_vars = {
    'JWKS_URL': JWKS_URL,
    'JWT_AUDIENCE': JWT_AUDIENCE,
    'JWT_ISSUER': JWT_ISSUER,
    'LITELLM_MASTER_KEY': LITELLM_MASTER_KEY,
}

missing_vars = [k for k, v in required_vars.items() if not v]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

# In-memory caches
jwks_cache = TTLCache(maxsize=1, ttl=600)  # Bound key-rotation lag to 10 minutes
user_key_cache = TTLCache(maxsize=1000, ttl=1800)  # Cache user keys for 30 minutes

# DynamoDB client
dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
user_key_table = dynamodb.Table(DYNAMODB_TABLE)

# Requests session with retries
session = requests.Session()
retry = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[500, 502, 503, 504],
)
# HTTP adapter mounted for the co-located LiteLLM sidecar (LITELLM_URL, default http://localhost:4000).
# All external calls (JWKS_URL) use HTTPS. Do not use this session for non-localhost HTTP endpoints.
session.mount('http://', HTTPAdapter(max_retries=retry))  # nosec # nosemgrep: python.lang.security.audit.insecure-transport.requests.request-session-with-http
session.mount('https://', HTTPAdapter(max_retries=retry))


def get_jwks(force_refresh: bool = False):
    """
    Fetch and cache JWKS (JSON Web Key Set) from IdP.

    This is used to verify JWT signature.
    """
    if not force_refresh and 'jwks' in jwks_cache:
        return jwks_cache['jwks']

    try:
        logger.info(f"Fetching JWKS from {JWKS_URL}")
        response = session.get(JWKS_URL, timeout=10)
        response.raise_for_status()
        jwks = response.json()
        if not isinstance(jwks, dict) or not isinstance(jwks.get('keys'), list):
            raise ValueError("JWKS response does not contain a keys array")
        jwks_cache['jwks'] = jwks
        return jwks
    except Exception as e:
        logger.warning(f"Failed to fetch JWKS: {e}")
        raise


def validate_jwt_token(token: str) -> Dict:
    """
    Validate JWT token and extract user information.

    Args:
        token: JWT token string (without 'Bearer ' prefix)

    Returns:
        Dict with user_id, email, and groups

    Raises:
        ValueError: If token is invalid
    """
    try:
        jwks = get_jwks()

        # Get the key ID from token header
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get('kid')

        if not kid:
            raise ValueError("Token missing 'kid' in header")

        # Refresh once on an unknown key so IdP signing-key rotation does not
        # require waiting for the normal cache TTL.
        key = next((k for k in jwks.get('keys', []) if k.get('kid') == kid), None)
        if not key:
            jwks = get_jwks(force_refresh=True)
            key = next((k for k in jwks.get('keys', []) if k.get('kid') == kid), None)
        if not key:
            raise ValueError(f"Key with kid '{kid}' not found in JWKS")

        payload = jwt.decode(
            token,
            key=jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key)),
            algorithms=['RS256'],
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={
                'verify_signature': True,
                'verify_exp': True,
                'verify_aud': True,
                'verify_iss': True,
            }
        )

        # Extract user information
        user_info = {
            'user_id': payload.get('sub'),
            'email': payload.get('email', payload.get('preferred_username')),
            'groups': payload.get('groups', []),
            'name': payload.get('name', ''),
            'department': payload.get('custom:department', payload.get('department', payload.get('dept', ''))),
            'team': payload.get('custom:team', payload.get('team', payload.get('team_id', ''))),
            'cost_center': payload.get('custom:cost_center', payload.get('cost_center', payload.get('costCenter', ''))),
            'organization': payload.get('custom:organization', payload.get('organization', '')),
            'location': payload.get('custom:location', payload.get('location', payload.get('office_location', ''))),
            'role': payload.get('custom:role', payload.get('role', payload.get('job_title', ''))),
            'manager': payload.get('custom:manager', payload.get('manager', payload.get('manager_email', ''))),
        }

        if not user_info['user_id']:
            raise ValueError("Token missing 'sub' claim (user ID)")

        logger.info(f"JWT validated for user: {user_info['email']}")
        return user_info

    except jwt.ExpiredSignatureError:
        raise ValueError("JWT token has expired")
    except jwt.InvalidAudienceError:
        raise ValueError(f"Invalid audience. Expected: {JWT_AUDIENCE}")
    except jwt.InvalidIssuerError:
        raise ValueError(f"Invalid issuer. Expected: {JWT_ISSUER}")
    except Exception as e:
        logger.error(f"JWT validation failed: {e}")
        raise ValueError(f"Invalid JWT token: {str(e)}")


def get_cached_api_key(user_id: str) -> Optional[str]:
    """
    Get cached API key for user from DynamoDB.

    Args:
        user_id: User ID from JWT

    Returns:
        API key string or None if not found
    """
    # Check in-memory cache first
    if user_id in user_key_cache:
        logger.debug(f"API key cache hit for user: {user_id}")
        return user_key_cache[user_id]

    # Check DynamoDB
    try:
        response = user_key_table.get_item(Key={'user_id': user_id})
        item = response.get('Item')

        if item:
            api_key = item.get('api_key')
            # Update in-memory cache
            user_key_cache[user_id] = api_key
            logger.info(f"API key found in DynamoDB for user: {user_id}")
            return api_key
    except Exception as e:
        logger.error(f"Failed to get API key from DynamoDB: {e}")
        raise RuntimeError("User key store is unavailable") from e


def cache_api_key(user_id: str, api_key: str, user_info: Dict):
    """
    Cache API key in DynamoDB and in-memory cache.

    Args:
        user_id: User ID from JWT
        api_key: LiteLLM API key
        user_info: Full user information from JWT
    """
    try:
        user_key_table.put_item(
            Item={
                'user_id': user_id,
                'api_key': api_key,
                'email': user_info.get('email'),
                'name': user_info.get('name'),
                'groups': user_info.get('groups', []),
                'department': user_info.get('department', ''),
                'team': user_info.get('team', ''),
                'cost_center': user_info.get('cost_center', ''),
                'organization': user_info.get('organization', ''),
                'location': user_info.get('location', ''),
                'role': user_info.get('role', ''),
                'manager': user_info.get('manager', ''),
                'created_at': int(time.time()),
                'ttl': int(time.time()) + (90 * 86400),  # 90 days TTL
            }
        )
        user_key_cache[user_id] = api_key
        logger.info(f"Cached API key in DynamoDB for user: {user_id}")
    except Exception as e:
        logger.error(f"Failed to cache API key in DynamoDB: {e}")
        raise RuntimeError("Failed to persist the generated API key") from e


def create_litellm_api_key(user_info: Dict) -> str:
    """
    Create a new API key in LiteLLM for the user.

    Args:
        user_info: User information from JWT

    Returns:
        LiteLLM API key string

    Raises:
        Exception: If key creation fails
    """
    try:
        url = f"{LITELLM_URL}/key/generate"
        headers = {
            'Authorization': f'Bearer {LITELLM_MASTER_KEY}',
            'Content-Type': 'application/json',
        }

        payload = {
            'key_alias': user_info['email'],
            'user_id': user_info['user_id'],
            'metadata': {
                'email': user_info['email'],
                'name': user_info['name'],
                'groups': user_info['groups'],
                'department': user_info.get('department', ''),
                'team': user_info.get('team', ''),
                'cost_center': user_info.get('cost_center', ''),
                'organization': user_info.get('organization', ''),
                'location': user_info.get('location', ''),
                'role': user_info.get('role', ''),
                'manager': user_info.get('manager', ''),
                'managed_by': 'jwt-middleware',
                'created_via': 'oidc-sso',
            }
        }

        logger.info(f"Creating API key for user: {user_info['email']}")
        response = session.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()

        data = response.json()
        api_key = data.get('key')

        if not api_key:
            raise ValueError("LiteLLM did not return an API key")

        logger.info(f"Successfully created API key for user: {user_info['email']}")
        return api_key

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 409:
            # Another task can win a concurrent create. Give it time to persist
            # the generated key in DynamoDB before returning an error.
            logger.warning(f"Key already exists for {user_info['email']}; waiting for key mapping")
            for _ in range(5):
                time.sleep(0.2)
                cached_key = get_cached_api_key(user_info['user_id'])
                if cached_key:
                    return cached_key
            raise RuntimeError("Key exists upstream but no user mapping is available") from e
        raise Exception(f"Failed to create API key: {e}")
    except Exception as e:
        logger.error(f"Failed to create API key: {e}")
        raise


def get_or_create_api_key(user_info: Dict) -> str:
    """
    Get existing API key for user or create a new one.

    Args:
        user_info: User information from JWT

    Returns:
        LiteLLM API key string
    """
    user_id = user_info['user_id']

    # Try cache first
    cached_key = get_cached_api_key(user_id)
    if cached_key:
        return cached_key

    # Create new key
    api_key = create_litellm_api_key(user_info)

    # Cache it
    cache_api_key(user_id, api_key, user_info)

    return api_key


def requires_jwt(f):
    """
    Decorator to validate JWT and inject user_info into request context.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Extract JWT from Authorization header
        auth_header = request.headers.get('Authorization', '')

        if not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Missing or invalid Authorization header'}), 401

        token = auth_header.split(' ', 1)[1]

        # Validate JWT
        try:
            user_info = validate_jwt_token(token)
            request.user_info = user_info
        except ValueError as e:
            # Log the specific reason server-side; return a generic message to
            # the client so token-validation internals are not exposed and we
            # don't reveal which check failed (CWE-209 / py/stack-trace-exposure).
            logger.warning(f"JWT validation failed: {e}")
            return jsonify({'error': 'Invalid or expired token'}), 401

        return f(*args, **kwargs)

    return decorated_function


@app.route('/health', methods=['GET'])
def health():
    """Readiness check for the upstream gateway and user key store."""
    checks = {}

    try:
        response = session.get(f"{LITELLM_URL}/health/liveliness", timeout=5)
        response.raise_for_status()
        checks['litellm'] = 'healthy'
    except Exception as e:
        logger.warning(f"LiteLLM readiness check failed: {e}")
        checks['litellm'] = 'unhealthy'

    try:
        user_key_table.load()
        checks['dynamodb'] = 'healthy'
    except Exception as e:
        logger.warning(f"DynamoDB readiness check failed: {e}")
        checks['dynamodb'] = 'unhealthy'

    status = 'healthy' if all(value == 'healthy' for value in checks.values()) else 'unhealthy'
    status_code = 200 if status == 'healthy' else 503
    return jsonify({'status': status, 'service': 'jwt-middleware', 'checks': checks}), status_code


@app.route('/health/live', methods=['GET'])
def health_live():
    """Process liveness check with no downstream dependency calls."""
    return jsonify({'status': 'healthy', 'service': 'jwt-middleware'}), 200


@app.route('/api/my-key', methods=['GET'])
@requires_jwt
def get_my_key():
    """
    Return API key for authenticated user.

    This endpoint is used by the self-service portal.
    """
    try:
        user_info = request.user_info
        api_key = get_or_create_api_key(user_info)

        return jsonify({
            'api_key': api_key,
            'user_id': user_info['user_id'],
            'email': user_info['email'],
            'message': 'Save this key - it will not be shown again!'
        }), 200

    except Exception as e:
        # Detail is logged server-side; the client gets a generic message so
        # internal error text is not exposed (CWE-209 / py/stack-trace-exposure).
        logger.error(f"Failed to get/create API key: {e}")
        return jsonify({'error': 'Key management failed'}), 500


@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])
@requires_jwt
def proxy(path):
    """
    Proxy all requests to LiteLLM after JWT validation.

    Validates JWT, gets/creates API key for user, and forwards request to LiteLLM.
    """
    try:
        user_info = request.user_info

        # Get or create API key for this user
        api_key = get_or_create_api_key(user_info)

        # Build LiteLLM URL
        litellm_url = f"{LITELLM_URL}/{path}"
        if request.query_string:
            litellm_url += f"?{request.query_string.decode()}"

        # Prepare headers
        headers = {k: v for k, v in request.headers.items()
                  if k.lower() not in ['host', 'connection']}
        headers['Authorization'] = f'Bearer {api_key}'

        # Forward request to LiteLLM
        logger.info(f"Proxying {request.method} /{path} for user {user_info['email']}")

        response = session.request(
            method=request.method,
            url=litellm_url,
            headers=headers,
            data=request.get_data(),
            stream=True,  # Important for streaming responses
            timeout=300,  # 5 minute timeout for long-running requests
        )

        # Stream response back to client
        def generate():
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        # This is an authenticated reverse proxy: the upstream (LiteLLM) body is
        # streamed back verbatim. Because the request path is client-controlled,
        # forward the body as a non-renderable type and tell the browser never to
        # MIME-sniff it into HTML, so a crafted path can't yield reflected XSS
        # (CWE-79 / py/reflective-xss). Codex consumes JSON / text/event-stream,
        # neither of which a browser renders as HTML.
        proxied_headers = {k: v for k, v in response.headers.items()
                           if k.lower() not in ['content-encoding', 'content-length',
                                                 'transfer-encoding', 'content-type',
                                                 'x-content-type-options']}
        proxied_headers['X-Content-Type-Options'] = 'nosniff'
        upstream_content_type = response.headers.get('Content-Type', 'application/octet-stream')

        return Response(
            generate(),
            status=response.status_code,
            headers=proxied_headers,
            content_type=upstream_content_type,
        )

    except Exception as e:
        # Detail is logged server-side; the client gets a generic message so
        # internal error text is not exposed (CWE-209 / py/stack-trace-exposure).
        logger.error(f"Proxy request failed: {e}")
        return jsonify({'error': 'Request failed'}), 500


if __name__ == '__main__':
    logger.info("Starting JWT Middleware")
    logger.info(f"LiteLLM URL: {LITELLM_URL}")
    logger.info(f"JWKS URL: {JWKS_URL}")
    logger.info(f"DynamoDB Table: {DYNAMODB_TABLE}")

    app.run(host='0.0.0.0', port=8080, debug=False)  # nosec B104 # nosemgrep: python.flask.security.audit.avoid_app_run_with_bad_host
