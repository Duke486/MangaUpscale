"""Exercise the Windows launcher using a small synthetic payload."""

import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import tomllib


def main():
    package = Path(__file__).resolve().parent
    version = tomllib.loads((package.parent / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    seven = shutil.which("7z")
    if not seven:
        seven = str(Path(os.environ["ProgramFiles"]) / "7-Zip/7z.exe")
    seven = Path(seven)
    if seven.parent.name == "shims":
        seven = seven.parent.parent / "apps/7zip/current/7z.exe"
    if not seven.with_name("7z.dll").is_file():
        seven = Path(os.environ["ProgramFiles"]) / "7-Zip/7z.exe"
    csc = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    with tempfile.TemporaryDirectory(prefix="manga-launcher-test-") as temp:
        work = Path(temp).resolve()
        fixture = work / "fixture"
        python = fixture / "runtime/python"
        python.mkdir(parents=True)
        (fixture / "bootstrap.py").write_text("# fixture\n")
        (fixture / "client-ready.ok").write_text("fixture")
        probe = work / "Probe.cs"
        probe.write_text(
            "using System.IO; class Probe { static void Main() { "
            'File.WriteAllText("probe.ok", "ready"); } }'
        )
        subprocess.run(
            [
                str(csc),
                "/nologo",
                "/target:winexe",
                "/out:" + str(python / "pythonw.exe"),
                str(probe),
            ],
            check=True,
        )
        payload = work / "fixture.7z"
        subprocess.run(
            [str(seven), "a", "-t7z", str(payload), "runtime", "bootstrap.py", "client-ready.ok"],
            cwd=fixture,
            check=True,
            capture_output=True,
        )
        content = payload.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        source = (package / "Launcher.cs").read_text(encoding="utf-8")
        parent = 'Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "MangaUpscale")'
        assert parent in source
        install_root = work / "installed"
        source = (
            source.replace("APP_VERSION", version)
            .replace(parent, '@"' + str(install_root) + '"')
            .replace("PAYLOAD_HASH", digest)
        )
        generated = work / "Launcher.cs"
        generated.write_text(source, encoding="utf-8")
        stub = work / "stub.exe"
        subprocess.run(
            [
                str(csc),
                "/nologo",
                "/target:winexe",
                "/platform:x64",
                "/reference:System.Windows.Forms.dll",
                "/reference:System.Drawing.dll",
                "/resource:" + str(seven) + ",7z.exe",
                "/resource:" + str(seven.with_name("7z.dll")) + ",7z.dll",
                "/out:" + str(stub),
                str(generated),
            ],
            check=True,
        )
        executable = work / "test.exe"
        executable.write_bytes(stub.read_bytes() + content + struct.pack("<q", len(content)))
        installed = install_root / (version + "-client")
        for _ in range(2):
            subprocess.run([str(executable), "--startup-test"], timeout=45, check=True)
            assert (installed / "installed.ok").read_text() == digest
            assert (installed / "probe.ok").read_text() == "ready"
            assert not (installed / ".tools").exists()
            (installed / "probe.ok").unlink()
    print("PASS: payload integrity, first extraction, repeated launch and cleanup")


if __name__ == "__main__":
    main()
