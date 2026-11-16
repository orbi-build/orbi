import json
from unittest.mock import Mock

import pytest

from orbi.body_steering import issue_body_editor
from seam import seam


@pytest.fixture
def graphql_read(monkeypatch):
    read = Mock()
    monkeypatch.setattr(seam, "run_gh_read_command", read)
    return read


@pytest.mark.parametrize(("body", "editor", "login"), [
    ("edited body", {"login": "alice"}, "alice"),
    ("", None, None),
    ("edited body", {"login": ""}, None),
    ("edited body", {"login": 42}, None),
    ("edited body", {}, None),
])
def test_issue_body_editor_reads_one_graphql_response(graphql_read, body, editor, login):
    graphql_read.return_value = json.dumps({"data": {"repository": {"issue": {
        "body": body, "editor": editor,
    }}}})

    assert issue_body_editor(1114, repo="owner/project", timeout=12) == (body, login)
    graphql_read.assert_called_once()
    (command,), kwargs = graphql_read.call_args
    executable, subcommand, endpoint, flag, query, *variables = command
    assert (executable, subcommand, endpoint, flag) == ("gh", "api", "graphql", "-f")
    assert variables == ["-f", "owner=owner", "-f", "name=project", "-F", "number=1114"]
    assert query.startswith("query=")
    query = "".join(query.split())
    assert 'repository(owner:$owner,name:$name){issue(number:$number){bodyeditor{login}}}' in query
    assert kwargs == {"timeout": 12}


@pytest.mark.parametrize("raw", [
    "not JSON",
    "null",
    '{"data":{"repository":null}}',
    '{"data":{"repository":{"issue":null}}}',
    '{"data":{"repository":{"issue":{"editor":null}}}}',
    '{"data":{"repository":{"issue":{"body":null,"editor":null}}}}',
    '{"data":{"repository":{"issue":{"body":"edited body"}}}}',
    '{"data":{"repository":{"issue":{"body":"edited body","editor":"alice"}}}}',
    '{"data":{"repository":{"issue":{"body":"edited body","editor":null}}},'
    '"errors":[{"message":"editor lookup failed"}]}',
])
def test_issue_body_editor_rejects_malformed_response(graphql_read, raw):
    graphql_read.return_value = raw

    with pytest.raises(ValueError, match="issue body editor"):
        issue_body_editor(1114, repo="owner/project", timeout=12)
