import ssl

import pytest

from tools import kubernetes_api
from tools import release_publish


def test_argo_workflow_collection_url_is_namespaced_and_https_only():
    assert kubernetes_api.argo_collection_url(
        "https://kubernetes.default.svc", "argo-workflows", "workflows"
    ) == (
        "https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/"
        "namespaces/argo-workflows/workflows"
    )
    with pytest.raises(release_publish.ReleaseError):
        kubernetes_api.argo_collection_url(
            "https://kubernetes.default.svc", "argo-workflows", "cronworkflows"
        )
    with pytest.raises(release_publish.ReleaseError):
        kubernetes_api.argo_collection_url(
            "https://kubernetes.default.svc", "Argo Workflows", "workflows"
        )
    for api_url in ("", "http://kubernetes.default.svc", "https://a:b@host", "https://host#x"):
        with pytest.raises(release_publish.ReleaseError):
            kubernetes_api.argo_collection_url(api_url, "argo-workflows", "workflows")


def test_service_account_token_is_read_from_the_projected_mount(tmp_path):
    (tmp_path / "token").write_text("projected-token\n", encoding="utf-8")

    assert kubernetes_api.read_service_account_token(tmp_path) == "projected-token"


@pytest.mark.parametrize("content", (None, "", "  \n"))
def test_missing_or_blank_service_account_token_is_an_error_without_a_value(
    tmp_path, content
):
    if content is not None:
        (tmp_path / "token").write_text(content, encoding="utf-8")

    with pytest.raises(release_publish.ReleaseError) as error:
        kubernetes_api.read_service_account_token(tmp_path)
    assert "ServiceAccount token" in str(error.value)


def test_request_function_pins_the_service_account_ca(tmp_path, monkeypatch):
    contexts = []

    def create_default_context(*, cafile=None):
        contexts.append(cafile)
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(kubernetes_api.ssl, "create_default_context", create_default_context)
    seen = {}

    def urlopen(request, **kwargs):
        seen.update(kwargs)
        raise OSError("stop before the network")

    monkeypatch.setattr(release_publish.urllib.request, "urlopen", urlopen)
    request = kubernetes_api.request_function(tmp_path)

    with pytest.raises(release_publish.ReleaseError):
        request(
            "GET",
            "https://kubernetes.example/apis",
            token="t",
            authorization_scheme="Bearer",
            service=kubernetes_api.SERVICE,
            sleep=lambda seconds: None,
        )
    assert contexts == [str(tmp_path / "ca.crt")]
    assert isinstance(seen["context"], ssl.SSLContext)


def test_missing_service_account_ca_is_an_error(tmp_path):
    with pytest.raises(release_publish.ReleaseError) as error:
        kubernetes_api.request_function(tmp_path)
    assert "ServiceAccount CA bundle is not usable" in str(error.value)
