"""Upload public moderation images without giving URLs access to internal hosts."""

import ipaddress
import mimetypes
import posixpath
import re
import socket
from urllib.parse import unquote, urljoin, urlsplit

import certifi
import urllib3
from django.conf import settings
from django.contrib.sites.models import Site
from django.core.files.storage import default_storage

MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


class ModerationImageError(Exception):
    pass


def read_uploaded_moderation_image(url):
    """Read only the site's pagedown uploads, never arbitrary storage objects.

    Return None for URLs outside the configured media location so callers can
    handle public external images separately.
    """
    try:
        media = urlsplit(settings.MEDIA_URL)
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            return None
        if parsed.netloc:
            if (
                parsed.scheme != "https"
                or not media.hostname
                or parsed.hostname != media.hostname
                or parsed.port not in (None, 443)
            ):
                return None
        elif parsed.scheme or not parsed.path.startswith("/"):
            return None
        base = media.path.rstrip("/") + "/"
        if not parsed.path.startswith(base):
            return None
        name = unquote(parsed.path[len(base) :])
        prefix = getattr(
            settings, "PAGEDOWN_IMAGE_UPLOAD_PATH", "pagedown-uploads"
        ).strip("/")
        parts = name.split("/")
        if (
            not name.startswith(prefix + "/")
            or any(part in ("", ".", "..") for part in parts)
            or "\\" in name
            or "%" in name
            or posixpath.normpath(name) != name
        ):
            return None
        content_type, _encoding = mimetypes.guess_type(name)
        if content_type not in IMAGE_TYPES:
            raise ModerationImageError("Unsupported uploaded image type")
        if default_storage.size(name) > MAX_IMAGE_BYTES:
            raise ModerationImageError("Image is empty or too large")
        with default_storage.open(name, "rb") as file:
            data = file.read(MAX_IMAGE_BYTES + 1)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ModerationImageError("Image is empty or too large")
        return data, content_type
    except ModerationImageError:
        raise
    except Exception as exc:
        raise ModerationImageError("Unable to read uploaded moderation image") from exc


def _upload_public_image(url, service):
    try:
        uploaded = read_uploaded_moderation_image(url)
        if uploaded is not None:
            data, content_type = uploaded
            extension = mimetypes.guess_extension(content_type) or ".jpg"
            return service._upload_bytes_to_poe(
                data, "moderation-image" + extension, "public moderation image"
            )
        domain = Site.objects.get_current().domain
        parsed = urlsplit(urljoin("https://%s/" % domain, url))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ModerationImageError("Unsupported image URL")
        host = parsed.hostname.encode("idna").decode("ascii")
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
        if not addresses or any(
            not ipaddress.ip_address(ip).is_global for ip in addresses
        ):
            raise ModerationImageError("Image address is not public")
        ip = sorted(addresses)[0]
        pool = urllib3.HTTPSConnectionPool(
            ip,
            port=443,
            server_hostname=host,
            assert_hostname=host,
            cert_reqs="CERT_REQUIRED",
            ca_certs=certifi.where(),
            timeout=urllib3.Timeout(connect=5, read=15),
            retries=False,
        )
        response = None
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            response = pool.request(
                "GET",
                path,
                headers={"Host": host},
                redirect=False,
                preload_content=False,
            )
            content_type = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            content_encoding = (
                response.headers.get("Content-Encoding", "").strip().lower()
            )
            if (
                response.status != 200
                or content_type not in IMAGE_TYPES
                or content_encoding not in ("", "identity")
            ):
                raise ModerationImageError("Unsupported image response")
            data = response.read(MAX_IMAGE_BYTES + 1, decode_content=False)
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise ModerationImageError("Image is empty or too large")
        finally:
            if response is not None:
                response.close()
            pool.close()
        extension = mimetypes.guess_extension(content_type) or ".jpg"
        return service._upload_bytes_to_poe(
            data, "moderation-image" + extension, "public moderation image"
        )
    except ModerationImageError:
        raise
    except Exception as exc:
        raise ModerationImageError("Unable to prepare moderation image") from exc


def embed_moderation_images(body, counter, service):
    matches = list(IMAGE_PATTERN.finditer(body))
    if len(matches) > 4:
        raise ModerationImageError("Too many moderation images")
    attachments = []

    def replace(match):
        attachment = _upload_public_image(match.group(1), service)
        if not attachment:
            raise ModerationImageError("Image upload failed")
        counter[0] += 1
        attachments.append(attachment)
        return "[image%d]" % counter[0]

    return IMAGE_PATTERN.sub(replace, body), attachments
