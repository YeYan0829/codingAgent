"""文件名不构成权限分类；路径越界仍由 test_path_guard 验证。"""
import pytest
from codeagent.safety.path_guard import PathGuard


@pytest.mark.parametrize("name", [".env", "id_rsa", "cert.pem", "key.key", "secret.json", "password_reset.py", "latex.tokens"])
def test_filenames_have_no_implicit_access_restrictions(tmp_path, name):
    path = tmp_path / name
    path.write_text("test fixture")
    assert PathGuard(tmp_path).resolve(name) == path
