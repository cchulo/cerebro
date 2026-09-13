import pathlib, pytest
from cerebro.core import load_config, AdapterContext, StaticLocator

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def example_config():
    return load_config(ROOT / "cerebro.example.yaml", env={})


@pytest.fixture
def ctx(example_config):
    class Secrets:
        def __init__(self): self.values = {}
        def get(self, name, default=None): return self.values.get(name, default)
    return AdapterContext(example_config, secrets=Secrets(), locator=StaticLocator(template="http://{unit}:8080"))
