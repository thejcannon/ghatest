from __future__ import annotations
import base64
from contextlib import contextmanager
import errno
import fnmatch
import glob
import hashlib


from pathlib import Path
import subprocess
import os
import re
import shutil
import tempfile
import zipfile
from xml.etree import ElementTree

import github
import requests

def get_pants_wheel_infos(tag_name, token):
    sha = requests.get(
        f"https://api.github.com/repos/pantsbuild/pants/commits/{tag_name}",
        headers={
            "Authorization": f"Bearer {token}",
        }
    ).json()["sha"]
    links = requests.get(
        f"https://binaries.pantsbuild.org/?prefix=wheels/pantsbuild.pants/{sha}"
    )
    links = ElementTree.fromstring(links.text)

    for element in links.findall("./{*}Contents/{*}Key"):
        if element.text.endswith(".whl"):
            yield f"https://binaries.pantsbuild.org/{element.text.replace('+', '%2b')}", element.text.rsplit("/", 1)[-1]

def get_pypi_whl_infos(version):
    for package in ["pantsbuild.pants", "pantsbuild.pants.testutil"]:
        for info in requests.get(f"https://pypi.org/pypi/{package}/{version}/json").json().get("urls", []):
            yield info["url"], info["filename"]

def _github():
    token = subprocess.run(
        ["gh", "auth", "token"], check=True, text=True, capture_output=True
    ).stdout.strip()
    return github.Github(auth=github.Auth.Token(token)), token

_version_re = re.compile(r"Version: (?P<version>\S+)")


@contextmanager
def open_zip(path_or_file, *args, **kwargs) :
    if not path_or_file:
        raise Exception(f"Invalid zip location: {path_or_file}")
    if "allowZip64" not in kwargs:
        kwargs["allowZip64"] = True
    try:
        zf = zipfile.ZipFile(path_or_file, *args, **kwargs)
    except zipfile.BadZipfile as bze:
        # Use the realpath in order to follow symlinks back to the problem source file.
        raise zipfile.BadZipfile(f"Bad Zipfile {os.path.realpath(path_or_file)}: {bze}")
    try:
        yield zf
    finally:
        zf.close()

def locate_dist_info_dir(workspace):
    dir_suffix = "*.dist-info"
    matches = glob.glob(os.path.join(workspace, dir_suffix))
    if not matches:
        raise Exception("Unable to locate `{}` directory in input whl.".format(dir_suffix))
    if len(matches) > 1:
        raise Exception("Too many `{}` directories in input whl: {}".format(dir_suffix, matches))
    return os.path.relpath(matches[0], workspace)

def any_match(globs, filename):
    return any(fnmatch.fnmatch(filename, g) for g in globs)

def read_file(filename: str, binary_mode: bool = False) -> bytes | str:
    mode = "rb" if binary_mode else "r"
    with open(filename, mode) as f:
        content: bytes | str = f.read()
        return content

def safe_delete(filename: str | Path) -> None:
    try:
        os.unlink(filename)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise

def safe_rmtree(directory: str | Path) -> None:
    if os.path.islink(directory):
        safe_delete(directory)
    else:
        shutil.rmtree(directory, ignore_errors=True)

def safe_mkdir(directory: str | Path, clean: bool = False) -> None:
    if clean:
        safe_rmtree(directory)
    try:
        os.makedirs(directory)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def safe_mkdir_for(path: str | Path, clean: bool = False) -> None:
    dirname = os.path.dirname(path)
    if dirname:
        safe_mkdir(dirname, clean=clean)

def safe_open(filename, *args, **kwargs):
    safe_mkdir_for(filename)
    return open(filename, *args, **kwargs)

def safe_file_dump(
    filename: str, payload: bytes | str = "", mode: str = "w", makedirs: bool = False
) -> None:
    if makedirs:
        os.makedirs(os.path.dirname(filename), exist_ok=True)
    with safe_open(filename, mode=mode) as f:
        f.write(payload)

def replace_in_file(workspace, src_file_path, from_str, to_str):
    from_bytes = from_str.encode("ascii")
    to_bytes = to_str.encode("ascii")
    data = read_file(os.path.join(workspace, src_file_path), binary_mode=True)
    if from_bytes not in data and from_str not in src_file_path:
        return None

    dst_file_path = src_file_path.replace(from_str, to_str)
    safe_file_dump(
        os.path.join(workspace, dst_file_path), data.replace(from_bytes, to_bytes), mode="wb"
    )
    if src_file_path != dst_file_path:
        os.unlink(os.path.join(workspace, src_file_path))
    return dst_file_path

def fingerprint_file(workspace, filename):
    content = read_file(os.path.join(workspace, filename), binary_mode=True)
    fingerprint = hashlib.sha256(content)
    record_encoded = base64.urlsafe_b64encode(fingerprint.digest()).rstrip(b"=")
    return f"sha256={record_encoded.decode()}", str(len(content))

def rewrite_record_file(workspace, src_record_file, mutated_file_tuples):
    mutated_files = set()
    dst_record_file = None
    for src, dst in mutated_file_tuples:
        if src == src_record_file:
            dst_record_file = dst
        else:
            mutated_files.add(dst)
    if not dst_record_file:
        raise Exception(
            "Malformed whl or bad globs: `{}` was not rewritten.".format(src_record_file)
        )

    output_records = []
    file_name = os.path.join(workspace, dst_record_file)
    for line in read_file(file_name).splitlines():
        filename, fingerprint_str, size_str = line.rsplit(",", 3)
        if filename in mutated_files:
            fingerprint_str, size_str = fingerprint_file(workspace, filename)
            output_line = ",".join((filename, fingerprint_str, size_str))
        else:
            output_line = line
        output_records.append(output_line)

    safe_file_dump(file_name, "\r\n".join(output_records) + "\r\n")

def reversion(
    *, whl_file: str, dest_dir: str, target_version: str, extra_globs: list[str] | None = None
) -> None:
    all_globs = ["*.dist-info/*", "*-nspkg.pth", *(extra_globs or ())]
    with tempfile.TemporaryDirectory() as workspace:
        # Extract the input.
        with open_zip(whl_file, "r") as whl:
            src_filenames = whl.namelist()
            whl.extractall(workspace)

        # Determine the location of the `dist-info` directory.
        dist_info_dir = locate_dist_info_dir(workspace)
        record_file = os.path.join(dist_info_dir, "RECORD")

        # Get version from the input whl's metadata.
        input_version = None
        metadata_file = os.path.join(workspace, dist_info_dir, "METADATA")
        with open(metadata_file, "r") as info:
            for line in info:
                mo = _version_re.match(line)
                if mo:
                    input_version = mo.group("version")
                    break
        if not input_version:
            raise Exception("Could not find `Version:` line in {}".format(metadata_file))

        # Rewrite and move all files (including the RECORD file), recording which files need to be
        # re-fingerprinted due to content changes.
        dst_filenames = []
        refingerprint = []
        for src_filename in src_filenames:
            if os.path.isdir(os.path.join(workspace, src_filename)):
                continue
            dst_filename = src_filename
            if any_match(all_globs, src_filename):
                rewritten = replace_in_file(workspace, src_filename, input_version, target_version)
                if rewritten is not None:
                    dst_filename = rewritten
                    refingerprint.append((src_filename, dst_filename))
            dst_filenames.append(dst_filename)

        # Refingerprint relevant entries in the RECORD file under their new names.
        rewrite_record_file(workspace, record_file, refingerprint)

        # Create a new output whl in the destination.
        dst_whl_filename = os.path.basename(whl_file).replace(input_version, target_version)
        dst_whl_file = os.path.join(dest_dir, dst_whl_filename)
        with tempfile.TemporaryDirectory() as chroot:
            tmp_whl_file = os.path.join(chroot, dst_whl_filename)
            with open_zip(tmp_whl_file, "w", zipfile.ZIP_DEFLATED) as whl:
                for dst_filename in dst_filenames:
                    whl.write(os.path.join(workspace, dst_filename), dst_filename)
            check_dst = os.path.join(chroot, "check-wheel")
            os.mkdir(check_dst)
            subprocess.run(args=[sys.executable, "-m", "wheel", "unpack", "-d", check_dst, tmp_whl_file], check=True)
            shutil.move(tmp_whl_file, dst_whl_file)
        print("Wrote whl with version {} to {}.\n".format(target_version, dst_whl_file))
    return dst_whl_file



def main(version_match) -> None:
    github, token = _github()
    repo = github.get_repo("pantsbuild/pants")
    releases = repo.get_releases()

    for release in releases:
        prefix, _, version = release.tag_name.partition("_")
        if prefix != "release" or not version:
            continue

        if version != version_match:
            continue

        name_to_id = {asset.name: asset.id for asset in release.assets}
        pypi_map = {filename: url for url, filename in get_pypi_whl_infos(version)}
        pants_map = {filename: url for url, filename in get_pants_wheel_infos(release.tag_name, token)}

        print(f"Uploading wheels for {version}")
        for filename, url in pants_map.items():
            reversioned_filename = re.sub(r"\+.*?-", "-", filename).replace('linux_', "manylinux2014_")
            if reversioned_filename in pypi_map:
                filename = reversioned_filename
                pypi = True
                url = pypi_map[filename]
            else:
                pypi = False

            print(f"Downloading {url}")
            for retry in range(5):
                try:
                    with open(filename, "wb") as f:
                        response = requests.get(url, stream=True)
                        response.raise_for_status()
                        for chunk in response.iter_content():
                            f.write(chunk)
                    break
                except Exception:
                    continue
            print(f"Downloaded {filename} from {url}")

            if not pypi:
                print(f"Reversioning {filename}")
                new_whl = reversion(
                    whl_file=filename,
                    dest_dir=".",
                    target_version=version,
                    extra_globs=["pants/_version/VERSION", "pants/VERSION"],
                )
                os.remove(filename)
                filename = new_whl.lstrip("./")
            else:
                print("PyPI release, skipping reversioning")

            if filename in name_to_id:
                response = requests.delete(f"https://api.github.com/repos/pantsbuild/pants/releases/assets/{name_to_id[filename]}",  headers={"Authorization": f"Bearer {token}"})

            print(f"Uploading {filename}")
            for retry in range(5):
                try:
                    with open(filename, "rb") as f:
                        response = requests.put(f"https://uploads.github.com/repos/pantsbuild/pants/releases/{release.id}/assets", params={"name": filename}, headers={"Content-Type": "application/octet-stream", "Authorization": f"Bearer {token}"}, data=f)
                        response.raise_for_status()
                    break
                except Exception:
                    continue

            os.remove(filename)


if __name__ == "__main__":
    import sys
    main((sys.argv[1:] + [""])[0])