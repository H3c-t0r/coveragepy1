# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/nedbat/coveragepy/blob/master/NOTICE.txt

"""Use the GitHub API to download built artifacts."""

import os
import os.path
import zipfile

import requests

def download_url(url, filename):
    """Download a file from `url` to `filename`."""
    response = requests.get(url, stream=True)
    if response.status_code == 200:
        with open(filename, "wb") as f:
            for chunk in response.iter_content(16*1024):
                f.write(chunk)

def unpack_zipfile(filename):
    """Unpack a zipfile, using the names in the zip."""
    with open(filename, "rb") as fzip:
        z = zipfile.ZipFile(fzip)
        for name in z.namelist():
            print(f"  extracting {name}")
            z.extract(name)

dest = "dist"
repo_owner = "nedbat/coveragepy"
temp_zip = "artifacts.zip"

if not os.path.exists(dest):
    os.makedirs(dest)
os.chdir(dest)

r = requests.get(f"https://api.github.com/repos/{repo_owner}/actions/artifacts")
latest = max(r.json()["artifacts"], key=lambda a: a["created_at"])
download_url(latest["archive_download_url"], temp_zip)
unpack_zipfile(temp_zip)
os.remove(temp_zip)
