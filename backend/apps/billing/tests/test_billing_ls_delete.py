"Public API."
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from apps.billing.views import LS_API_BASE, _ls_delete

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(LS_API_KEY="test-ls-api-key-contract")
def test_ls_delete_builds_exact_delete_request_and_reads_body_cleanly():
    mock_resp = MagicMock()
    mock_resp.read.return_value = b""
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False

    with patch("apps.billing.views.urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        _ls_delete("/subscriptions/sub_contract_1", timeout=(3, 10))

    mock_urlopen.assert_called_once()
    (req,), kwargs = mock_urlopen.call_args

    # exact URL: base + /subscriptions/{id} — no path mangling.
    assert req.full_url == f"{LS_API_BASE}/subscriptions/sub_contract_1"
    # HTTP method is DELETE (urllib defaults to GET/POST based on data;
    # _ls_delete must explicitly set method="DELETE").
    assert req.get_method() == "DELETE"
    # Authorization: Bearer <key> present, using the configured LS_API_KEY.
    assert req.get_header("Authorization") == "Bearer test-ls-api-key-contract"
    # the summed connect+read timeout, exactly as _ls_get/_ls_patch do.
    assert kwargs["timeout"] == sum((3, 10)) == 13
    # a successful empty-body read returns cleanly (no json.loads attempted
    # on an empty DELETE response, unlike _ls_get/_ls_post/_ls_patch).
    mock_resp.read.assert_called_once()
