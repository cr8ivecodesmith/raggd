set shell := ["bash", "-lc"]
set dotenv-load := true
set dotenv-filename := ".env"
set export := true


## Config

HERE := justfile_directory()
MARKER_DIR := HERE
VERSION := `awk -F\" '/^version/{print $2}' pyproject.toml`
PYTHON_VERSION := trim(read(".python-version"))
PYTHON_VERSIONS := `awk -F'[^0-9]+' '/requires-python/{for(i=$3;i<$5;)printf(i-$3?" ":"")$2"."i++}' pyproject.toml`
#BUILD_WHEEL_FILE := `ls dist/raggd*.whl 2>/dev/null | head -n 1`


## Recipes

@version:
    echo "{{ VERSION }}"

@python-versions:
    echo "Development Python version: {{ PYTHON_VERSION }}"
    echo "Supported Python versions: {{ PYTHON_VERSIONS }}"

@build-clean:
    rm -rf dist build


@build:
    just build-clean
    uv build

test:
    uv run pytest

lint:
    uv run ruff check --fix src tests
    uv run ruff format src tests

lint-check:
    uv run ruff check src tests
