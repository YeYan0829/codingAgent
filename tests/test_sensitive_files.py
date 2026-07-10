import pytest

from codeagent.safety.path_guard import PathGuard, PathGuardError


@pytest.mark.parametrize("name", [".env", "id_rsa", "token_config.txt", "secret.json", "credentials.ini"])
def test_sensitive_files_are_blocked(tmp_path, name):
    (tmp_path / name).write_text("x", encoding="utf-8")

    with pytest.raises(PathGuardError):
        PathGuard(tmp_path).resolve(name)


@pytest.mark.parametrize("name", [".env.example", ".env.sample", ".env.template"])
def test_env_template_files_are_readable_by_default(tmp_path, name):
    path = tmp_path / name
    path.write_text("EXAMPLE=value", encoding="utf-8")

    assert PathGuard(tmp_path).resolve(name) == path.resolve()
