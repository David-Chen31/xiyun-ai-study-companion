"""Create a static-only client distribution."""
import argparse
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from frontend_server import CLIENT_ASSETS, ROOT


def build(output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output.parent, suffix=".zip", delete=False) as file:
        staged = Path(file.name)
    try:
        with ZipFile(staged, "w", compression=ZIP_DEFLATED) as bundle:
            bundle.write(ROOT / "personal/index.html", "index.html")
            for name in CLIENT_ASSETS:
                bundle.write(ROOT / name, name)
        staged.replace(output)
    finally:
        staged.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="打包仅包含个人端静态资源的 ZIP")
    parser.add_argument("--out", type=Path, default=ROOT.parent / ".xiyun" / "personal-client.zip")
    output = parser.parse_args().out
    build(output)
    print(output.resolve())


if __name__ == "__main__":
    main()
