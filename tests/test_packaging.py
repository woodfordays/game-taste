"""The files that decide what the Docker image installs and sends.

The image installs `constraints.txt` first and then the app with `--no-deps`, so a
dependency missing from the constraints would be missing from the image, and the build
would still pass. These tests keep the two lists in step.
"""

import importlib.metadata
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.!+_-]*$")


def pins() -> dict[str, str]:
    """Package name (canonical) to pinned version, from constraints.txt."""
    found = {}
    for line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            name, version = line.split("==")
            found[canonicalize_name(name)] = version
    return found


def direct_dependencies() -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return [Requirement(text) for text in project["project"]["dependencies"]]


def lines(name: str) -> list[str]:
    text = (ROOT / name).read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip()]


def test_every_line_of_the_constraints_is_an_exact_pin():
    for line in lines("constraints.txt"):
        if not line.startswith("#"):
            assert PIN.match(line), f"not an exact pin: {line}"


def test_every_direct_dependency_is_pinned():
    pinned = pins()
    for requirement in direct_dependencies():
        assert canonicalize_name(requirement.name) in pinned, requirement.name


def test_the_extras_of_a_dependency_are_pinned_too():
    # uvicorn[standard] brings more packages; --no-deps would leave them out otherwise.
    # A package that the current platform doesn't install (uvloop on Windows) is skipped.
    pinned = pins()
    for requirement in direct_dependencies():
        for text in importlib.metadata.requires(requirement.name) or []:
            extra = Requirement(text)
            if extra.marker is None:
                continue
            for name in requirement.extras:
                wanted = extra.marker.evaluate({"extra": name})
                always = extra.marker.evaluate({"extra": ""})
                if wanted and not always:
                    assert canonicalize_name(extra.name) in pinned, extra.name


def test_development_tools_are_not_in_the_images_list():
    assert "pytest" not in pins()


def test_the_dockerfile_installs_the_pins_first_and_the_app_without_dependencies():
    docker = lines("Dockerfile")
    steps = [line for line in docker if line.split()[0] in ("COPY", "RUN")]
    assert steps == [
        "COPY constraints.txt .",
        "RUN pip install --no-cache-dir -r constraints.txt",
        "COPY . .",
        "RUN pip install --no-cache-dir --no-deps .",
    ]


def test_the_image_and_the_repository_leave_out_data_and_keys():
    for name in (".gitignore", ".dockerignore"):
        listed = lines(name)
        for pattern in ("data*/", "*.db", "secret.key", "session.secret"):
            assert pattern in listed, (name, pattern)
    ignored = lines(".dockerignore")
    for pattern in (".superpowers/", "tests/", "docs/", ".venv/", ".git"):
        assert pattern in ignored, pattern
    assert "constraints.txt" not in ignored  # the build needs it
