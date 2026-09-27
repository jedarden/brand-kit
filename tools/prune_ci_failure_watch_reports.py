#!/usr/bin/env python3
"""Prune expired failure-watch reports from the Garage S3 bucket.

The watcher writes one report per Workflow UID, so Argo's ``artifactGC`` cannot
provide a useful retention boundary: deleting an individual Workflow must not
delete the report that an owner may still need.  This small S3 client keeps the
pruning step dependency-free and uses separate read and write credentials:
listing needs read access, while deletion needs write access.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree

DEFAULT_ENDPOINT = "https://s3.ardenone.com"
DEFAULT_BUCKET = "needle-ci-artifacts"
DEFAULT_PREFIX = "failures/brand-kit-ci-failure-watch/v1/"
DEFAULT_RETENTION_DAYS = 30
AWS_SERVICE = "s3"
AWS_TERMINATOR = "aws4_request"
S3_XML_NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"


class RetentionError(RuntimeError):
    """Raised when the retention pass cannot safely complete."""


@dataclass(frozen=True)
class S3Credentials:
    access_key: str
    secret_key: str


@dataclass(frozen=True)
class S3Object:
    key: str
    last_modified: datetime


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _date_stamp(value: datetime) -> str:
    return value.strftime("%Y%m%d")


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="-_.~")


def _canonical_query(query: list[tuple[str, str]]) -> str:
    return "&".join(
        f"{_quote(key)}={_quote(value)}"
        for key, value in sorted(query, key=lambda item: (_quote(item[0]), _quote(item[1])))
    )


def _canonical_uri(path: str) -> str:
    return "/" + "/".join(_quote(part) for part in path.split("/") if part)


def _sign(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _namespace_tag(root: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == name]


def _tag_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element.iter():
        if child.tag.rsplit("}", 1)[-1] == name:
            return child.text
    return None


def _parse_s3_timestamp(value: str | None) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RetentionError("Garage returned an object without LastModified")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise RetentionError("Garage returned an invalid LastModified timestamp") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class S3Client:
    """Minimal path-style S3 client for the two calls used by pruning."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        credentials: S3Credentials,
        *,
        region: str = "garage",
        opener=urllib.request.urlopen,
        now: datetime | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(endpoint.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RetentionError("S3 endpoint must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise RetentionError("S3 endpoint must not include a query or fragment")
        if not bucket or "/" in bucket:
            raise RetentionError("S3 bucket name is malformed")
        if not region:
            raise RetentionError("S3 region is required")
        self._endpoint = parsed
        self._bucket = bucket
        self._credentials = credentials
        self._region = region
        self._opener = opener
        self._now = now

    def _request(
        self,
        method: str,
        query: list[tuple[str, str]],
        *,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
    ) -> bytes:
        current = _utc_now(self._now)
        amz_date = _timestamp(current)
        date_stamp = _date_stamp(current)
        body_hash = hashlib.sha256(body).hexdigest()
        path = "/".join(
            part for part in (self._endpoint.path.strip("/"), self._bucket) if part
        )
        path = f"/{path}"
        canonical_uri = _canonical_uri(path)
        canonical_query = _canonical_query(query)
        headers = {
            "Host": self._endpoint.netloc,
            "X-Amz-Content-Sha256": body_hash,
            "X-Amz-Date": amz_date,
        }
        if extra_headers:
            headers.update(extra_headers)
        canonical_headers = "".join(
            f"{key.lower()}:{' '.join(value.strip().split())}\n"
            for key, value in sorted(headers.items(), key=lambda item: item[0].lower())
        )
        signed_headers = ";".join(sorted(key.lower() for key in headers))
        canonical_request = "\n".join(
            (
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                body_hash,
            )
        )
        credential_scope = f"{date_stamp}/{self._region}/{AWS_SERVICE}/{AWS_TERMINATOR}"
        string_to_sign = "\n".join(
            (
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        signing_key = _sign(
            _sign(
                _sign(
                    _sign(
                        f"AWS4{self._credentials.secret_key}".encode("utf-8"),
                        date_stamp,
                    ),
                    self._region,
                ),
                AWS_SERVICE,
            ),
            AWS_TERMINATOR,
        )
        signature = hmac.new(
            signing_key,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["Authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._credentials.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        url = urllib.parse.urlunsplit(
            (
                self._endpoint.scheme,
                self._endpoint.netloc,
                path,
                canonical_query,
                "",
            )
        )
        request = urllib.request.Request(url, data=body or None, headers=headers, method=method)
        try:
            with self._opener(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            error.close()
            raise RetentionError(
                f"Garage {method} request failed with HTTP {error.code}"
            ) from error
        except urllib.error.URLError as error:
            raise RetentionError("cannot reach Garage during report pruning") from error

    def list_objects(self, prefix: str) -> list[S3Object]:
        objects: list[S3Object] = []
        continuation_token: str | None = None
        while True:
            query = [("list-type", "2"), ("prefix", prefix)]
            if continuation_token:
                query.append(("continuation-token", continuation_token))
            try:
                root = ElementTree.fromstring(self._request("GET", query))
            except ElementTree.ParseError as error:
                raise RetentionError("Garage returned malformed object-list XML") from error
            for contents in _namespace_tag(root, "Contents"):
                key = _tag_text(contents, "Key")
                if not isinstance(key, str) or not key.startswith(prefix):
                    continue
                objects.append(
                    S3Object(
                        key=key,
                        last_modified=_parse_s3_timestamp(_tag_text(contents, "LastModified")),
                    )
                )
            is_truncated = _tag_text(root, "IsTruncated") == "true"
            continuation_token = _tag_text(root, "NextContinuationToken")
            if not is_truncated:
                return objects
            if not continuation_token:
                raise RetentionError("Garage truncated an object list without a continuation token")

    def delete_objects(self, keys: list[str]) -> None:
        for offset in range(0, len(keys), 1000):
            batch = keys[offset : offset + 1000]
            root = ElementTree.Element(f"{{{S3_XML_NAMESPACE}}}Delete")
            for key in batch:
                element = ElementTree.SubElement(root, f"{{{S3_XML_NAMESPACE}}}Object")
                ElementTree.SubElement(element, f"{{{S3_XML_NAMESPACE}}}Key").text = key
            body = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
            content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
            try:
                response = ElementTree.fromstring(
                    self._request(
                        "POST",
                        [("delete", "")],
                        body=body,
                        extra_headers={
                            "Content-MD5": content_md5,
                            "Content-Type": "application/xml",
                        },
                    )
                )
            except ElementTree.ParseError as error:
                raise RetentionError("Garage returned malformed delete XML") from error
            errors = _namespace_tag(response, "Error")
            if errors:
                raise RetentionError(f"Garage refused deletion of {len(errors)} report object(s)")


def prune_reports(
    reader: S3Client,
    publisher: S3Client,
    *,
    prefix: str = DEFAULT_PREFIX,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: datetime | None = None,
) -> tuple[int, datetime]:
    if not isinstance(retention_days, int) or isinstance(retention_days, bool):
        raise RetentionError("retention days must be an integer")
    if not 1 <= retention_days <= 3650:
        raise RetentionError("retention days must be between 1 and 3650")
    if not prefix.endswith("/"):
        raise RetentionError("report prefix must end with /")
    cutoff = _utc_now(now) - timedelta(days=retention_days)
    expired = [
        item
        for item in reader.list_objects(prefix)
        if item.key.endswith("/report.json") and item.last_modified < cutoff
    ]
    publisher.delete_objects([item.key for item in expired])
    return len(expired), cutoff


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RetentionError(f"{name} is required; provide it through the environment")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prune expired brand-kit CI failure reports.")
    parser.add_argument("--endpoint", default=os.environ.get("S3_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--region", default=os.environ.get("S3_REGION", "garage"))
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        now = _utc_now(None)
        reader = S3Client(
            args.endpoint,
            args.bucket,
            S3Credentials(
                _required_environment("S3_READER_ACCESS_KEY"),
                _required_environment("S3_READER_SECRET_KEY"),
            ),
            region=args.region,
            now=now,
        )
        publisher = S3Client(
            args.endpoint,
            args.bucket,
            S3Credentials(
                _required_environment("S3_PUBLISHER_ACCESS_KEY"),
                _required_environment("S3_PUBLISHER_SECRET_KEY"),
            ),
            region=args.region,
            now=now,
        )
        pruned, _ = prune_reports(
            reader,
            publisher,
            prefix=args.prefix,
            retention_days=args.retention_days,
            now=now,
        )
    except (RetentionError, ValueError) as error:
        print(f"ERROR  failure-watch report pruning did not complete: {error}", file=sys.stderr)
        return 2
    print(
        f"PRUNED  {pruned} failure-watch report(s) older than "
        f"{args.retention_days} days"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
