import os
import subprocess
from xml.etree import ElementTree
import github
import requests
import urllib



def _github():
    token = subprocess.run(
        ["gh", "auth", "token"], check=True, text=True, capture_output=True
    ).stdout.strip()
    return github.Github(auth=github.Auth.Token(token)), token

gh, token = _github()
repo = gh.get_repo("pantsbuild/pants")

def do_one(release):
    tag = release.tag_name
    prefix, _, version = release.tag_name.partition("_")

    assets = {asset.name for asset in release.assets}

    USES_PYTHON_39 = int(version.split(".")[1]) >= 5  # Pants 2.5 was Py 3.9
    pyver = "cp39" if USES_PYTHON_39 else "cp38"

    wheel_to_pex_map = {
        f"pantsbuild.pants-{version}-{pyver}-{pyver}-macosx_10_11_x86_64.whl": f"pants.{version}-{pyver}-darwin_x86_64.pex",
        f"pantsbuild.pants-{version}-{pyver}-{pyver}-macosx_10_15_x86_64.whl": f"pants.{version}-{pyver}-darwin_x86_64.pex",
        f"pantsbuild.pants-{version}-{pyver}-{pyver}-macosx_11_0_arm64.whl": f"pants.{version}-{pyver}-darwin_arm64.pex",
        f"pantsbuild.pants-{version}-{pyver}-{pyver}-manylinux2014_aarch64.whl": f"pants.{version}-{pyver}-linux_aarch64.pex",
        f"pantsbuild.pants-{version}-{pyver}-{pyver}-manylinux2014_x86_64.whl": f"pants.{version}-{pyver}-linux_x86_64.pex",
    }

    wheel_to_pex_map = {
        key: value for key, value in wheel_to_pex_map.items() if key in assets
    }

    commit_sha = repo._requester.requestJsonAndCheck("GET", f"{repo.url}/git/refs/tags/{tag}")[1]["object"]["sha"]

    try:
        list_bucket_results = requests.get(f"https://binaries.pantsbuild.org?prefix=wheels/3rdparty/{commit_sha[:8]}").content
    except Exception:
        commit_sha = repo._requester.requestJsonAndCheck("GET", f"{repo.url}/git/tags/{tag}")[1]['object']["sha"]
        list_bucket_results = requests.get(f"https://binaries.pantsbuild.org?prefix=wheels/3rdparty/{commit_sha[:8]}").content

    with open("links.html", "w") as fp:
        # N.B.: S3 bucket listings use a default namespace. Although the URI is apparently stable,
        # we decouple from it with the wildcard.
        for key in ElementTree.fromstring(list_bucket_results).findall("./{*}Contents/{*}Key"):
            bucket_path = str(key.text)
            fp.write(
                f'<a href="https://binaries.pantsbuild.org/{urllib.parse.quote(bucket_path)}">'
                f"{os.path.basename(bucket_path)}"
                f"</a>\n"
            )

        # AHA!
        list_bucket_results = requests.get("https://binaries.pantsbuild.org/?prefix=wheels/3rdparty/852f420").content
        for key in ElementTree.fromstring(list_bucket_results).findall("./{*}Contents/{*}Key"):
            bucket_path = str(key.text)
            fp.write(
                f'<a href="https://binaries.pantsbuild.org/{urllib.parse.quote(bucket_path)}">'
                f"{os.path.basename(bucket_path)}"
                f"</a>\n"
            )
        # AHA!
        list_bucket_results = requests.get("https://binaries.pantsbuild.org/?prefix=wheels/3rdparty/869d82ed").content
        for key in ElementTree.fromstring(list_bucket_results).findall("./{*}Contents/{*}Key"):
            bucket_path = str(key.text)
            fp.write(
                f'<a href="https://binaries.pantsbuild.org/{urllib.parse.quote(bucket_path)}">'
                f"{os.path.basename(bucket_path)}"
                f"</a>\n"
            )

        fp.flush()

    for wheel_name, pex_name in wheel_to_pex_map.items():
        if pex_name in assets:
            continue

        platform = wheel_name.rsplit(".", 1)[0].rsplit("-", 1)[-1].replace("manylinux2014", "linux")
        print(f"TRYING TO BUILD: {pex_name}")
        subprocess.run(
            [
                "pex",
                "--disable-cache",
                "--python-shebang",
                "/usr/bin/env python",
                "-o",
                pex_name,
                "-f",
                "https://wheels.pantsbuild.org/simple",
                "-f",
                "links.html",
                f"pantsbuild.pants=={version}",
                "--no-build",
                "--disable-cache",
                "--no-strip-pex-env",
                "--console-script=pants",
                "--venv",
                f"--platform={platform}-cp-{pyver[2:]}-{pyver}",
            ],
        )

        if not os.path.exists(pex_name):
            continue

        print(f"Uploading {pex_name}")
        for retry in range(5):
            try:
                with open(pex_name, "rb") as f:
                    response = requests.put(f"https://uploads.github.com/repos/pantsbuild/pants/releases/{release.id}/assets", params={"name": pex_name}, headers={"Content-Type": "application/octet-stream", "Authorization": f"Bearer {token}"}, data=f)
                    response.raise_for_status()
                break
            except Exception:
                continue

versions = {
"release_2.17.0.dev2",
"release_2.17.0.dev1",
"release_2.17.0.dev0",
}

def main():
    #releases = repo.get_releases()

    for release_tag in versions:
        release = repo.get_release(release_tag)

        do_one(release)

if __name__ == "__main__":
    import sys
    main()
