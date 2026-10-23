"""Held-out checks for the ``auto_error`` to ``raise_on_missing`` migration."""

import inspect
import typing
import warnings

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.security import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    HTTPBasic,
    HTTPBearer,
    HTTPDigest,
    OAuth2,
    OAuth2AuthorizationCodeBearer,
    OAuth2PasswordBearer,
    OpenIdConnect,
)
from fastapi.security.http import HTTPBase
from fastapi.testclient import TestClient

FACTORIES = {
    "APIKeyQuery": lambda **kw: APIKeyQuery(name="k", **kw),
    "APIKeyHeader": lambda **kw: APIKeyHeader(name="k", **kw),
    "APIKeyCookie": lambda **kw: APIKeyCookie(name="k", **kw),
    "HTTPBase": lambda **kw: HTTPBase(scheme="custom", **kw),
    "HTTPBasic": lambda **kw: HTTPBasic(**kw),
    "HTTPBearer": lambda **kw: HTTPBearer(**kw),
    "HTTPDigest": lambda **kw: HTTPDigest(**kw),
    "OAuth2": lambda **kw: OAuth2(flows={}, **kw),
    "OAuth2PasswordBearer": lambda **kw: OAuth2PasswordBearer(tokenUrl="token", **kw),
    "OAuth2AuthorizationCodeBearer": lambda **kw: OAuth2AuthorizationCodeBearer(
        authorizationUrl="auth", tokenUrl="token", **kw
    ),
    "OpenIdConnect": lambda **kw: OpenIdConnect(openIdConnectUrl="/openid", **kw),
}
NAMES = sorted(FACTORIES)


def client_for(scheme) -> TestClient:
    app = FastAPI()

    @app.get("/")
    def read(value=Depends(scheme)):
        return {"value": None if value is None else "present"}

    return TestClient(app)


@pytest.mark.parametrize("name", NAMES)
def test_default_still_rejects_missing_credentials(name):
    response = client_for(FACTORIES[name]()).get("/")
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


@pytest.mark.parametrize("name", NAMES)
def test_explicit_true_rejects_missing_credentials(name):
    response = client_for(FACTORIES[name](raise_on_missing=True)).get("/")
    assert response.status_code == 401


@pytest.mark.parametrize("name", NAMES)
def test_false_makes_missing_credentials_optional(name):
    response = client_for(FACTORIES[name](raise_on_missing=False)).get("/")
    assert response.status_code == 200
    assert response.json() == {"value": None}


@pytest.mark.parametrize("name", NAMES)
def test_old_keyword_warns_once_at_the_caller_and_still_works(name):
    with pytest.warns(DeprecationWarning) as record:
        scheme = FACTORIES[name](auto_error=False)
    deprecations = [w for w in record if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1, [str(w.message) for w in deprecations]
    message = str(deprecations[0].message)
    assert "auto_error" in message and "raise_on_missing" in message
    assert deprecations[0].filename == __file__
    assert client_for(scheme).get("/").json() == {"value": None}
    with pytest.warns(DeprecationWarning):
        strict = FACTORIES[name](auto_error=True)
    assert client_for(strict).get("/").status_code == 401


@pytest.mark.parametrize("name", NAMES)
def test_both_keywords_are_rejected_even_when_they_agree(name):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(TypeError):
            FACTORIES[name](raise_on_missing=True, auto_error=True)
        with pytest.raises(TypeError):
            FACTORIES[name](raise_on_missing=False, auto_error=True)


@pytest.mark.parametrize("name", NAMES)
def test_attribute_alias_reads_and_writes_without_warning(name):
    scheme = FACTORIES[name](raise_on_missing=False)
    assert scheme.raise_on_missing is False
    assert scheme.auto_error is False
    scheme.auto_error = True
    assert scheme.raise_on_missing is True
    assert client_for(scheme).get("/").status_code == 401
    scheme.raise_on_missing = False
    assert scheme.auto_error is False
    assert client_for(scheme).get("/").json() == {"value": None}


def test_third_party_subclass_reading_auto_error_keeps_working():
    class Custom(HTTPBearer):
        async def __call__(self, request: Request):
            if not self.auto_error:
                return "fallback"
            return await super().__call__(request)

    optional = client_for(Custom(raise_on_missing=False))
    assert optional.get("/").json() == {"value": "present"}
    strict = client_for(Custom())
    assert strict.get("/").status_code == 401


def test_wrong_scheme_branches_honour_the_new_keyword():
    headers = {"Authorization": "Basic abc"}
    assert client_for(HTTPBearer()).get("/", headers=headers).status_code == 401
    optional = client_for(HTTPBearer(raise_on_missing=False))
    assert optional.get("/", headers=headers).json() == {"value": None}
    digest = client_for(HTTPDigest(raise_on_missing=False))
    assert digest.get("/", headers=headers).json() == {"value": None}
    basic = client_for(HTTPBasic(raise_on_missing=False))
    assert basic.get("/", headers={"Authorization": "Bearer abc"}).json() == {"value": None}
    oauth = client_for(OAuth2PasswordBearer(tokenUrl="t", raise_on_missing=False))
    assert oauth.get("/", headers=headers).json() == {"value": None}


def test_present_credentials_are_unaffected():
    ok = client_for(HTTPBearer(raise_on_missing=False)).get(
        "/", headers={"Authorization": "Bearer abc"}
    )
    assert ok.json() == {"value": "present"}
    header = client_for(APIKeyHeader(name="k", raise_on_missing=False))
    assert header.get("/", headers={"k": "v"}).json() == {"value": "present"}


def test_openapi_security_schemes_are_unchanged():
    app = FastAPI()
    bearer = HTTPBearer(scheme_name="Bear", raise_on_missing=False)
    key = APIKeyQuery(name="k", scheme_name="Key", raise_on_missing=False)

    @app.get("/")
    def read(a=Depends(bearer), b=Depends(key)):
        return {}

    schemes = TestClient(app).get("/openapi.json").json()["components"]["securitySchemes"]
    assert schemes == {
        "Bear": {"type": "http", "scheme": "bearer"},
        "Key": {"type": "apiKey", "name": "k", "in": "query"},
    }


# Every scheme but HTTPBase documented auto_error with a Doc annotation.
@pytest.mark.parametrize("name", [name for name in NAMES if name != "HTTPBase"])
def test_parameter_docs_use_the_new_name(name):
    scheme = type(FACTORIES[name]())
    annotation = inspect.signature(scheme.__init__).parameters["raise_on_missing"].annotation
    docs = [item.documentation for item in typing.get_args(annotation)[1:] if hasattr(item, "documentation")]
    assert docs, f"{name}.raise_on_missing has no Doc"
    assert "`raise_on_missing` is set to `False`" in " ".join(docs[0].split())
    assert "`auto_error` is set" not in " ".join(docs[0].split())
