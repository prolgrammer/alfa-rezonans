import pytest

from app.api import accounts as accounts_module
from app.api.accounts import AccountsError, load_accounts
from app.api.live import LiveBackend
from app.contracts import Access, SessionCtx, UserRole
from app.settings import REPO_ROOT

GOOD = """
companies:
  C-1: Компания один
accounts:
  - login: a
    company_id: C-1
    user_role: cfo
    access: client
    interests: [key_rate]
"""


def write(tmp_path, text):
    path = tmp_path / "demo_accounts.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_real_file_loads():
    result = load_accounts(REPO_ROOT / "config" / "demo_accounts.yaml")
    assert {a.access for a in result.accounts} == set(Access)
    assert all(a.company_id for a in result.accounts)


def test_good_file(tmp_path):
    result = load_accounts(write(tmp_path, GOOD))
    assert result.companies == {"C-1": "Компания один"}
    assert result.accounts[0].user_role is UserRole.cfo


@pytest.mark.parametrize(
    ("bad", "fragment"),
    [
        ("user_role: cfo", "user_role: wizard"),
        ("access: client", "access: root"),
        ("interests: [key_rate]", "interests: [no_such_factor]"),
        ("company_id: C-1", "company_id: ''"),
        ("login: a", "login: ''"),
        ("    company_id: C-1\n", ""),
    ],
)
def test_broken_file_names_the_account(tmp_path, bad, fragment):
    with pytest.raises(AccountsError) as caught:
        load_accounts(write(tmp_path, GOOD.replace(bad, fragment)))
    message = str(caught.value)
    assert "demo_accounts.yaml" in message
    if "login: ''" not in fragment:
        assert "'a'" in message


def test_duplicate_login(tmp_path):
    text = GOOD + "  - login: a\n    company_id: C-1\n    user_role: owner\n    access: client\n"
    with pytest.raises(AccountsError, match="повторяются логины: a"):
        load_accounts(write(tmp_path, text))


def test_missing_file(tmp_path):
    with pytest.raises(AccountsError):
        load_accounts(tmp_path / "none.yaml")


async def test_company_name_and_has_company_from_file_without_db(monkeypatch):
    async def no_pool():
        raise AssertionError("пул не должен вызываться")

    monkeypatch.setattr("app.api.live.get_pool", no_pool)
    accounts_module.demo_accounts.cache_clear()
    try:
        backend = LiveBackend()
        known = SessionCtx(user_id=1, company_id="C-DEV-01", user_role=UserRole.cfo, access=Access.client)
        other = known.model_copy(update={"company_id": "C-X"})
        assert await backend.company_name(known) == "ООО «Стройинвест» (демо)"
        assert await backend.company_name(other) == "C-X"
        assert await backend.has_company("C-IT-01") is True
    finally:
        accounts_module.demo_accounts.cache_clear()
