from pathlib import Path

import sofias_memory


def test_package_is_importable() -> None:
    assert sofias_memory.__name__ == "sofias_memory"


def test_package_file_is_root_package_init() -> None:
    package_file = Path(sofias_memory.__file__)

    assert package_file.name == "__init__.py"
    assert package_file.parent.name == "sofias_memory"
