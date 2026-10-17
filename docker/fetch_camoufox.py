"""Install the pinned Camoufox browser and keep only its Linux font profile."""

import hashlib
import shutil

from camoufox.addons import DefaultAddons, maybe_download_addons
from camoufox.locale import ALLOW_GEOIP, download_mmdb
from camoufox.pkgman import INSTALL_DIR, OS_NAME, CamoufoxFetcher, Version


# Same browser release as the existing working image. Update the hashes with it.
VERSION = Version(version="152.0.4", release="beta.30")
SHA256 = {
    "x86_64": "5720d45b894ce1770543de024c6f10d514b38be560fa2dc3226b3d8586caf672",
    "arm64": "60447260af8bebdb0ec3f2aa72f687b879e5598367303de2e3fdbc7a5be8c124",
}


class PinnedCamoufoxFetcher(CamoufoxFetcher):
    def get_asset(self):
        if OS_NAME != "lin" or self.arch not in SHA256:
            raise RuntimeError("The compact image supports Linux x86_64 and arm64")
        if not VERSION.is_supported():
            raise RuntimeError("Pinned browser is incompatible with the Camoufox Python package")
        name = VERSION.full_string
        url = f"https://github.com/daijro/camoufox/releases/download/v{name}/camoufox-{name}-lin.{self.arch}.zip"
        return VERSION, url

    def extract_zip(self, zip_file):
        digest = hashlib.file_digest(zip_file, "sha256").hexdigest()
        if digest != SHA256[self.arch]:
            raise RuntimeError("Camoufox archive checksum does not match the pinned release")
        zip_file.seek(0)
        super().extract_zip(zip_file)


def main():
    PinnedCamoufoxFetcher().install()
    if not (INSTALL_DIR / "fonts" / "linux").is_dir():
        raise RuntimeError("Camoufox font layout changed; review Linux-only packaging before updating")
    for profile in ("windows", "macos"):
        shutil.rmtree(INSTALL_DIR / "fonts" / profile)
        shutil.rmtree(INSTALL_DIR / "fontconfig" / profile)
    if ALLOW_GEOIP:
        download_mmdb()
    maybe_download_addons(list(DefaultAddons))
    print(f"Camoufox {VERSION.full_string} installed with Linux fonts")


if __name__ == "__main__":
    main()
