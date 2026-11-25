"""Build and bundle the pinned in-process llama.cpp encoder for a desktop target."""
from hashlib import file_digest
import os
from pathlib import Path
import shutil
import subprocess
from tarfile import open as open_tar
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.request import urlopen
from zipfile import ZipFile

HERE = Path(__file__).resolve().parent
RELEASE = "b12283"
COMMIT = "f3b407ca1bfeeecebf9e9ef19fcd6eed565bd70a"
UPSTREAM = "https://github.com/ggml-org/llama.cpp/releases/download/" + RELEASE
SOURCE_HASH = "2b90e0164fed14efee90335327f6975d0d4728d81761b2568ad367dbf59c9193"
MAC_HASH = "bbc2abe2001293687540fb274bf4114b27b56af1fc166ee76730f64996d68b00"
WIN_HASH = "dafb19ac757bfb7cfc1f91c6e26703264df861c4331e3ef6dc9543ae8d010c5e"


def _archive(cache, url, digest):
    destination = cache / digest
    if destination.exists():
        with destination.open("rb") as f:
            if file_digest(f, "sha256").hexdigest() == digest:
                return destination
    cache.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=cache, delete=False) as tmp:
        part = Path(tmp.name)
    try:
        with urlopen(url, timeout=120) as response, part.open("wb") as f:
            shutil.copyfileobj(response, f)
        with part.open("rb") as f:
            if file_digest(f, "sha256").hexdigest() != digest:
                raise ValueError(f"Checksum mismatch for {url}")
        os.replace(part, destination)
    finally:
        part.unlink(missing_ok=True)
    return destination


def install_qwen_runtime(resources, native_dir, *, windows=False, zig="zig", cache=None):
    """Return the staged libraries for signing. No server/CLI executable is included."""
    native_dir.mkdir(parents=True, exist_ok=True)
    licenses = resources / "licenses" / "Qwen3"
    shutil.copytree(HERE / "qwen-licenses", licenses, dirs_exist_ok=True)
    if cache is None:
        cache = Path.home() / ".cache" / "seekino-build" / "llama.cpp"
    with TemporaryDirectory() as name:
        work = Path(name)
        src = _archive(cache, f"https://codeload.github.com/ggml-org/llama.cpp/tar.gz/{COMMIT}", SOURCE_HASH)
        with open_tar(src) as tar:
            prefix = f"llama.cpp-{COMMIT}/"
            for member in tar.getmembers():
                p = Path(member.name)
                if member.isfile() and p.suffix == ".h" and str(p.parent) in (prefix + "include", prefix + "ggml/include"):
                    (work / p.name).write_bytes(tar.extractfile(member).read())
            for source, target in (("LICENSE", "llama.cpp-MIT.txt"), ("licenses/LICENSE-jsonhpp", "jsonhpp-MIT.txt")):
                (licenses / target).write_bytes(tar.extractfile(prefix + source).read())
        common = ["-std=c11", "-shared", "-O2", "-Wall", "-Werror", "-I", str(work), str(HERE / "qwen_embed.c")]
        libs = []
        if windows:
            archive = _archive(cache, f"{UPSTREAM}/llama-{RELEASE}-bin-win-cpu-x64.zip", WIN_HASH)
            with ZipFile(archive) as z:
                for item in z.namelist():
                    if item in ("llama.dll", "ggml.dll", "ggml-base.dll", "libomp.dll") or (
                        item.startswith("ggml-cpu-") and item.endswith(".dll") and Path(item).name == item
                    ):
                        path = native_dir / item
                        path.write_bytes(z.read(item))
                        libs.append(path)
                (licenses / "LLVM-OpenMP.txt").write_bytes(z.read("LICENSE-LLVM-OpenMP"))
            target = native_dir / "seekino-qwen.dll"
            command = [zig, "cc", "-target", "x86_64-windows-gnu", *common, str(native_dir / "llama.dll"),
                       str(native_dir / "ggml.dll"), "-o", str(target)]
        else:
            archive = _archive(cache, f"{UPSTREAM}/llama-{RELEASE}-bin-macos-arm64.tar.gz", MAC_HASH)
            with open_tar(archive) as tar:
                for library in ("llama", "ggml", "ggml-base", "ggml-cpu", "ggml-blas", "ggml-metal", "ggml-rpc"):
                    file = f"lib{library}.0.dylib"
                    # Upstream versioned links point to the full-version member.
                    path = native_dir / file
                    path.write_bytes(tar.extractfile(f"llama-{RELEASE}/{file}").read())
                    libs.append(path)
            target = native_dir / "libseekino-qwen.dylib"
            command = ["xcrun", "clang", "-arch", "arm64", "-mmacosx-version-min=15.0", *common,
                       str(native_dir / "libllama.0.dylib"), str(native_dir / "libggml.0.dylib"),
                       "-Wl,-install_name,@rpath/libseekino-qwen.dylib", "-Wl,-rpath,@loader_path", "-o", str(target)]
        subprocess.run(command, check=True, cwd=work)
        return [*libs, target]
