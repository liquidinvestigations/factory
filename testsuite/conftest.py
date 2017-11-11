import sys
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from pathlib import Path
import pytest

repo = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(repo)]

import factory as factory_module

default_paths = factory_module.paths


def pytest_addoption(parser):
    parser.addoption('--image', help="use existing image for tests")


@contextmanager
def monkeypatcher():
    from _pytest.monkeypatch import MonkeyPatch
    mocks = MonkeyPatch()
    try:
        yield mocks
    finally:
        mocks.undo()


@contextmanager
def tmpdir_factory():
    with TemporaryDirectory() as tmp:
        tmp_repo = Path(tmp)

        with monkeypatcher() as mocks:

            class FactoryWrapper:

                def __init__(self):
                    self.images = tmp_repo / 'images'
                    self.images.mkdir()

                    self.shared = tmp_repo / 'shared'
                    self.shared.mkdir()

                    tmp_paths = factory_module.Paths(tmp_repo)
                    mocks.setattr(factory_module, 'paths', tmp_paths)
                    self.main = factory_module.main

            yield FactoryWrapper()


@pytest.fixture(scope='session')
def cloud_image(pytestconfig):
    image_name = pytestconfig.getoption('--image')

    if image_name:
        yield default_paths.IMAGES / image_name

    else:
        with tmpdir_factory() as factory:
            factory.main(['prepare-cloud-image'])
            [image] = factory.images.iterdir()
            yield image


@pytest.fixture
def factory(cloud_image):
    with tmpdir_factory() as factory:
        (factory.images / cloud_image.name).symlink_to(cloud_image)
        yield factory
