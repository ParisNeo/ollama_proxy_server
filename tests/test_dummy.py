"""Dummy test to ensure pytest runs without failures."""


def test_dummy():
    """A dummy test that always passes."""
    assert True


def test_imports():
    """Test that main modules can be imported."""
    from app.main import app

    assert app is not None


def test_config():
    """Test that configuration loads properly."""
    from app.core.config import settings

    assert settings is not None
