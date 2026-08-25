from pathlib import Path

import obs_platform


def test_package_imports_from_src_layout() -> None:
    package_path = Path(obs_platform.__file__).resolve()

    assert package_path.parts[-3:-1] == ("src", "obs_platform")
