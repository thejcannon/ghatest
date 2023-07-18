import github
import requests
import subprocess

def get_wheel_infos(version):
    for package in ["pantsbuild.pants", "pantsbuild.pants.testutil"]:
        for info in requests.get(f"https://pypi.org/pypi/{package}/{version}/json").json().get("urls", []):
            yield info["url"], info["filename"]


def _github():
    token = subprocess.run(
        ["gh", "auth", "token"], check=True, text=True, capture_output=True
    ).stdout.strip()
    return github.Github(auth=github.Auth.Token(token)), token


def main(version_match) -> None:
    github, token = _github()
    repo = github.get_repo("pantsbuild/pants")
    releases = repo.get_releases()

    for release in releases:
        prefix, _, version = release.tag_name.partition("_")
        if prefix != "release" or not version:
            continue

        if version.count(".") != 2:
            continue

        if not version.startswith(version_match):
            continue

        wheel_infos = get_wheel_infos(version)
        if not wheel_infos:
            continue

        print(f"Uploading wheels for {version}")
        assets = {asset.name for asset in release.assets}
        print(assets)
        for url, filename in list(wheel_infos):
            if filename in assets:
                continue

            print(f"Uploading {filename} to {version} using {url}")
            with open("foo.whl", "wb") as f:
                response = requests.get(url, stream=True)
                response.raise_for_status()
                for chunk in response.iter_content():
                    f.write(chunk)

            with open("foo.whl", "rb") as f:
                response = requests.post(f"https://uploads.github.com/repos/pantsbuild/pants/releases/{release.id}/assets", params={"name": filename}, headers={"Content-Type": "application/octet-stream", "Authorization": f"Bearer {token}"}, data=f)
                response.raise_for_status()

if __name__ == "__main__":
    import sys
    main((sys.argv[1:] + [""])[0])