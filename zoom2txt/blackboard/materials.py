"""Extract downloadable Blackboard files from Ultra document BBML.

Ultra document bodies expose Content Collection links rather than the Original
course attachments endpoint. URLs stay in memory; identity excludes signatures.
See https://blackboard.github.io/rest-apis/learn/examples/curl-attachments-demo
"""

import json
import mimetypes
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse


def material_url(base: str, value: str) -> str:
    """Accept only an institutional Content Collection URL discovered in BBML."""
    url = urljoin(base + "/", value.strip())
    parsed, origin = urlparse(url), urlparse(base)
    path = unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != origin.netloc.lower()
        or parsed.username
        or parsed.password
        or not path.startswith("/bbcswebdav/")
        or "\\" in url
        or any(c in url for c in "\r\n\t")
        or any(part in (".", "..") for part in path.split("/"))
    ):
        raise ValueError(
            "El archivo incrustado no tiene un enlace institucional de descarga válido."
        )
    return url


@dataclass
class EmbeddedFile:
    id: str
    filename: str
    url: str = field(repr=False)


class DocumentFiles(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.files: list[EmbeddedFile] = []
        self.issues: list[str] = []
        self.external_links = 0
        self.has_text = False
        self._anchor = None
        self._ignored = 0
        self._seen = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in ("script", "style"):
            self._ignored += 1
            return
        if self._ignored:
            return
        if tag == "a":
            if self._anchor:
                self._add(*self._anchor)
            self._anchor = (values, "")
        elif tag in ("img", "video", "audio", "source", "iframe", "object", "embed"):
            if "data-mathml" not in values:
                self._add(values, values.get("alt", ""))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._ignored = max(0, self._ignored - 1)
        elif tag == "a" and self._anchor:
            self._add(*self._anchor)
            self._anchor = None

    def handle_data(self, data):
        if self._ignored:
            return
        if self._anchor:
            attrs, text = self._anchor
            self._anchor = (attrs, text + data)
        elif data.strip():
            self.has_text = True

    def close(self):
        super().close()
        if self._anchor:
            self._add(*self._anchor)
            self._anchor = None

    def _add(self, attrs, text):
        value = attrs.get("href") or attrs.get("src") or attrs.get("data") or ""
        if not value or value.startswith("#"):
            return
        try:
            url = material_url(self.base, value)
        except ValueError:
            # An external resource remains visible as unsupported; never send
            # this institution's cookies to an arbitrary embedded destination.
            if "data-bbfile" in attrs or "/bbcswebdav/" in value:
                self.issues.append("Un archivo incrustado requiere abrirse desde Blackboard.")
            elif urlparse(value).scheme not in ("data", "javascript", "mailto"):
                self.external_links += 1
            return
        path = unquote(urlparse(url).path)
        match = re.search(r"(?:xid-|rid-)(\d+_\d+)(?:/|$)", path)
        if not match:
            self.issues.append("No se reconoció el identificador de un archivo incrustado.")
            return
        identity = "xid-" + match.group(1)
        if identity in self._seen:
            return
        metadata = {}
        try:
            metadata = json.loads(attrs.get("data-bbfile", "{}"))
        except (ValueError, TypeError):
            pass
        if not isinstance(metadata, dict):
            metadata = {}
        filename = next(
            (
                str(v).strip()
                for v in (
                    metadata.get("fileName"),
                    metadata.get("linkName"),
                    text,
                    metadata.get("alternativeText"),
                    attrs.get("title"),
                    attrs.get("alt"),
                )
                if isinstance(v, str) and v.strip()
            ),
            identity,
        )
        if not re.search(r"\.[A-Za-z0-9]{1,8}$", filename):
            extension = mimetypes.guess_extension(str(metadata.get("mimeType", ""))) or ""
            filename += extension
        self._seen.add(identity)
        self.files.append(EmbeddedFile(identity, filename, url))


def document_files(base: str, body: str) -> DocumentFiles:
    if not isinstance(body, str):
        raise ValueError("El contenido del documento no tiene un formato válido.")
    parser = DocumentFiles(base)
    parser.feed(body)
    parser.close()
    return parser
