from pathlib import Path

from skyvern.forge.sdk.routes.browser_profiles import _copy_browser_profile_template
from skyvern.webeye.profile_cookie_merge import BANKED_COOKIES_FILENAME
from skyvern.webeye.session_cookies import SESSION_COOKIES_FILENAME


def test_copy_browser_profile_template_excludes_cookie_sidecars(tmp_path: Path) -> None:
    # A new empty (API-created) profile is seeded from a base template; it must never inherit login-cookie
    # sidecars, or a contaminated template bakes another run's (cross-org) cookies permanently into it.
    source = tmp_path / "template"
    (source / "Default").mkdir(parents=True)
    (source / "Default" / "Preferences").write_text("{}")
    (source / "Local State").write_text("{}")
    (source / SESSION_COOKIES_FILENAME).write_text("[]")
    (source / BANKED_COOKIES_FILENAME).write_text("[]")

    dest = tmp_path / "profile"
    dest.mkdir()
    _copy_browser_profile_template(source, dest)

    copied = {p.name for p in dest.iterdir()}
    assert SESSION_COOKIES_FILENAME not in copied
    assert BANKED_COOKIES_FILENAME not in copied
    assert "Local State" in copied  # a normal file still copies, so the exclusion is targeted
    assert (dest / "Default" / "Preferences").exists()
