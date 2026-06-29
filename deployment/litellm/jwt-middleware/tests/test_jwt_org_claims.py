"""
Tests for org attribute extraction in validate_jwt_token().

Covers three claim patterns per IdP:
  - Standard flat claims (Okta / Entra)
  - custom: prefixed claims (Cognito)
  - Fallback aliases (dept, costCenter, office_location, job_title, manager_email)
"""
import json
import time
import unittest
from unittest.mock import patch, MagicMock
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
import jwt as pyjwt


def _make_rsa_keypair():
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    return private_key, private_key.public_key()


PRIVATE_KEY, PUBLIC_KEY = _make_rsa_keypair()
KID = "test-key-1"


def _sign(claims: dict) -> str:
    base = {
        "sub": "user-123",
        "email": "alice@example.com",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    base.update(claims)
    return pyjwt.encode(base, PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})


def _jwks_for(public_key) -> dict:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    import base64, struct

    pub_numbers = public_key.public_key().public_numbers() if hasattr(public_key, "private_bytes") else public_key.public_numbers()

    def _b64(n, length=256):
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

    return {
        "keys": [{
            "kty": "RSA",
            "kid": KID,
            "use": "sig",
            "alg": "RS256",
            "n": _b64(pub_numbers.n),
            "e": _b64(pub_numbers.e, 3),
        }]
    }


JWKS = _jwks_for(PRIVATE_KEY)


def _load_app():
    """Import app with required env vars set and AWS mocked."""
    import importlib, sys, os

    os.environ.setdefault("JWKS_URL", "https://idp.example.com/.well-known/jwks.json")
    os.environ.setdefault("LITELLM_MASTER_KEY", "sk-test")

    mock_boto3 = MagicMock()
    mock_boto3.resource.return_value.Table.return_value = MagicMock()

    mock_requests = MagicMock()
    mock_requests_adapters = MagicMock()
    mock_requests_adapters.HTTPAdapter = MagicMock
    mock_requests.adapters = mock_requests_adapters

    mock_urllib3 = MagicMock()
    mock_urllib3_util = MagicMock()
    mock_urllib3_util_retry = MagicMock()
    mock_urllib3_util_retry.Retry = MagicMock

    sys.modules.setdefault("boto3", mock_boto3)
    sys.modules.setdefault("requests", mock_requests)
    sys.modules.setdefault("requests.adapters", mock_requests_adapters)
    sys.modules.setdefault("urllib3", mock_urllib3)
    sys.modules.setdefault("urllib3.util", mock_urllib3_util)
    sys.modules.setdefault("urllib3.util.retry", mock_urllib3_util_retry)
    sys.modules.setdefault("flask", MagicMock())
    sys.modules.setdefault("cachetools", MagicMock())

    import pathlib, importlib.util
    app_path = pathlib.Path(__file__).parents[1] / "app.py"
    spec = importlib.util.spec_from_file_location("jwt_middleware_app", app_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestOrgClaimExtraction(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _load_app()

    def _validate(self, extra_claims: dict) -> dict:
        token = _sign(extra_claims)
        with patch.object(self.app, "get_jwks", return_value=JWKS):
            return self.app.validate_jwt_token(token)

    # ------------------------------------------------------------------ #
    # Standard flat claims (Okta / Entra style)                           #
    # ------------------------------------------------------------------ #

    def test_standard_department(self):
        info = self._validate({"department": "Engineering"})
        self.assertEqual(info["department"], "Engineering")

    def test_standard_team(self):
        info = self._validate({"team": "platform"})
        self.assertEqual(info["team"], "platform")

    def test_standard_cost_center(self):
        info = self._validate({"cost_center": "CC-9001"})
        self.assertEqual(info["cost_center"], "CC-9001")

    def test_standard_organization(self):
        info = self._validate({"organization": "ACME"})
        self.assertEqual(info["organization"], "ACME")

    def test_standard_location(self):
        info = self._validate({"location": "Seattle"})
        self.assertEqual(info["location"], "Seattle")

    def test_standard_role(self):
        info = self._validate({"role": "developer"})
        self.assertEqual(info["role"], "developer")

    def test_standard_manager(self):
        info = self._validate({"manager": "bob@example.com"})
        self.assertEqual(info["manager"], "bob@example.com")

    # ------------------------------------------------------------------ #
    # Cognito custom: prefix claims                                        #
    # ------------------------------------------------------------------ #

    def test_cognito_department(self):
        info = self._validate({"custom:department": "Finance"})
        self.assertEqual(info["department"], "Finance")

    def test_cognito_team(self):
        info = self._validate({"custom:team": "data"})
        self.assertEqual(info["team"], "data")

    def test_cognito_cost_center(self):
        info = self._validate({"custom:cost_center": "CC-1234"})
        self.assertEqual(info["cost_center"], "CC-1234")

    def test_cognito_organization(self):
        info = self._validate({"custom:organization": "Corp"})
        self.assertEqual(info["organization"], "Corp")

    def test_cognito_location(self):
        info = self._validate({"custom:location": "NYC"})
        self.assertEqual(info["location"], "NYC")

    def test_cognito_role(self):
        info = self._validate({"custom:role": "lead"})
        self.assertEqual(info["role"], "lead")

    def test_cognito_manager(self):
        info = self._validate({"custom:manager": "carol@example.com"})
        self.assertEqual(info["manager"], "carol@example.com")

    # ------------------------------------------------------------------ #
    # Fallback alias claims                                                #
    # ------------------------------------------------------------------ #

    def test_alias_dept(self):
        info = self._validate({"dept": "Legal"})
        self.assertEqual(info["department"], "Legal")

    def test_alias_team_id(self):
        info = self._validate({"team_id": "squad-42"})
        self.assertEqual(info["team"], "squad-42")

    def test_alias_costCenter(self):
        info = self._validate({"costCenter": "CC-9999"})
        self.assertEqual(info["cost_center"], "CC-9999")

    def test_alias_office_location(self):
        info = self._validate({"office_location": "London"})
        self.assertEqual(info["location"], "London")

    def test_alias_job_title(self):
        info = self._validate({"job_title": "Staff Engineer"})
        self.assertEqual(info["role"], "Staff Engineer")

    def test_alias_manager_email(self):
        info = self._validate({"manager_email": "mgr@example.com"})
        self.assertEqual(info["manager"], "mgr@example.com")

    # ------------------------------------------------------------------ #
    # Priority: custom: beats flat beats alias                            #
    # ------------------------------------------------------------------ #

    def test_custom_prefix_takes_priority_over_flat(self):
        info = self._validate({"custom:department": "Wins", "department": "Loses"})
        self.assertEqual(info["department"], "Wins")

    def test_flat_takes_priority_over_alias(self):
        info = self._validate({"department": "Wins", "dept": "Loses"})
        self.assertEqual(info["department"], "Wins")

    # ------------------------------------------------------------------ #
    # Empty / absent claims produce empty string (not None / KeyError)    #
    # ------------------------------------------------------------------ #

    def test_missing_org_claims_default_to_empty_string(self):
        info = self._validate({})
        for field in ("department", "team", "cost_center", "organization", "location", "role", "manager"):
            with self.subTest(field=field):
                self.assertEqual(info[field], "", f"Expected '' for missing '{field}', got {info[field]!r}")

    # ------------------------------------------------------------------ #
    # Core identity fields still present                                  #
    # ------------------------------------------------------------------ #

    def test_user_id_and_email_still_extracted(self):
        info = self._validate({"department": "Eng"})
        self.assertEqual(info["user_id"], "user-123")
        self.assertEqual(info["email"], "alice@example.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
