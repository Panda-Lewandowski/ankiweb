from pathlib import Path

def test_shell_bundle_built():
    out = Path(__file__).resolve().parent.parent / "ankiweb/shell/static/bootstrap.js"
    assert out.exists(), "run: npm install && npm run build"
    assert b"WebSocket" in out.read_bytes()

def test_shell_bundle_has_nav_helpers():
    out = Path(__file__).resolve().parent.parent / "ankiweb/shell/static/bootstrap.js"
    data = out.read_bytes()
    assert b"ankiwebNavigate" in data
    assert b"anki-opchanges" in data

def test_bootstrap_has_opchanges_optout():
    from pathlib import Path
    js = Path("ankiweb/shell/static/bootstrap.js").read_text()
    assert "__ankiwebOnOpchanges" in js


def test_security_bundle_adds_csrf_to_same_origin_mutations():
    out = Path(__file__).resolve().parent.parent / "ankiweb/shell/static/security.js"
    assert out.exists(), "run: npm install && npm run build"
    data = out.read_text()
    assert "X-CSRF-Token" in data
    assert "ankiweb_csrf" in data
    assert "_csrf" in data
