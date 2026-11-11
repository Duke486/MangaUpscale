"""Read ZIP pages or EPUB raster pages in publication order, without extracting files."""

from pathlib import PurePosixPath
import posixpath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from control import check

ARCHIVES = {".zip", ".cbz", ".epub"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def member_path(base, href):
    url = urlsplit(href)
    path = unquote(url.path)
    if url.scheme or url.netloc or not path or "\\" in path or ":" in path or "\0" in path:
        raise ValueError("unsafe_member")
    if path.startswith("/"):
        raise ValueError("unsafe_member")
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), path))
    if path == ".." or path.startswith("../"):
        raise ValueError("unsafe_member")
    return path


def archive_entries(archive, epub=False):
    from pipeline import natural

    entries = [entry for entry in archive.infolist() if not entry.is_dir()]
    if len({entry.filename for entry in entries}) != len(entries):
        raise ValueError("duplicate_zip_names")
    if not epub:
        return sorted(entries, key=lambda entry: natural(entry.filename))

    def document(path):
        check()
        if archive.getinfo(path).file_size > 4 * 1024**2:
            raise ValueError("invalid_epub")
        return ET.fromstring(archive.read(path))

    def image(path):
        if PurePosixPath(path).suffix.lower() not in IMAGES:
            raise ValueError("unsupported_epub_image")
        return archive.getinfo(path)

    try:
        container = document("META-INF/container.xml")
        rootfile = container.find("./{*}rootfiles/{*}rootfile")
        if rootfile is None or not rootfile.get("full-path"):
            raise ValueError("invalid_epub")
        package_path = member_path("", rootfile.get("full-path"))
        package = document(package_path)
        manifest = {item.get("id"): item for item in package.findall("./{*}manifest/{*}item")}
        pages = []
        for ref in package.findall("./{*}spine/{*}itemref"):
            check()
            if ref.get("linear") == "no":
                continue
            item = manifest[ref.get("idref")]
            path = member_path(package_path, item.attrib["href"])
            media = item.get("media-type")
            if media in ("application/xhtml+xml", "image/svg+xml"):
                for node in document(path).iter():
                    tag = node.tag.rsplit("}", 1)[-1]
                    href = None
                    if tag == "img":
                        href = node.get("src")
                    elif tag == "image":
                        href = node.get("href") or node.get("{http://www.w3.org/1999/xlink}href")
                    if href:
                        pages.append(image(member_path(path, href)))
            elif media and media.startswith("image/"):
                pages.append(image(path))
            else:
                raise ValueError("invalid_epub")
        if not pages:
            raise ValueError("archive_has_no_pages")
        return pages
    except (ET.ParseError, KeyError) as error:
        raise ValueError("invalid_epub") from error
