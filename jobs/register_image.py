"""Register a Cisco IOS-XE image into Nautobot from the firmware server.

Companion to the upgrade job. The actual .bin files are hosted by the companion
"nautobot-composer" stack's `firmware` profile: engineers upload via a
Filebrowser UI, and a read-only nginx "firmware-download" service serves the same
files to devices. Nautobot is only the index.

This Job takes the uploaded file name, builds the DEVICE-FACING download URL from
a configurable base, validates the image is reachable (preferring the worker's
internal route to the firmware-download service), and records it as a core
``dcim.SoftwareImageFile`` mapped to the compatible device types — so the upgrade
job can consume it. It does NOT upload the file; publish it via Filebrowser first.

NOTE: brand new, not yet validated end-to-end. Run with Dry-run first.
"""

from __future__ import annotations

import hashlib
import os

import requests
from django.db import transaction
from nautobot.apps.jobs import (
    BooleanVar,
    ChoiceVar,
    DryRunVar,
    Job,
    MultiObjectVar,
    ObjectVar,
    StringVar,
)
from nautobot.dcim.choices import SoftwareImageFileHashingAlgorithmChoices
from nautobot.dcim.models import DeviceType, SoftwareImageFile, SoftwareVersion
from nautobot.extras.models import Status

from . import constants as C

name = "IOS-XE Upgrades"


class RegisterAbort(Exception):
    """A validation step failed; abort the registration."""


class RegisterImage(Job):
    """Validate a firmware image and register it in Nautobot."""

    image_file_name = StringVar(
        description=(
            "Exact uploaded file name (canonical Cisco name preserved), e.g. "
            "cat9k_iosxe.17.09.04.SPA.bin."
        ),
    )
    software_version = ObjectVar(
        model=SoftwareVersion,
        required=True,
        description="The Software Version this image provides.",
    )
    device_types = MultiObjectVar(
        model=DeviceType,
        required=False,
        description="Device types this image is compatible with (recommended).",
    )
    image_status = ObjectVar(
        model=Status,
        required=True,
        query_params={"content_types": "dcim.softwareimagefile"},
        description="Status for the Software Image File record.",
    )
    default_image = BooleanVar(
        default=False,
        description="Mark as the default image for this version (unsets any other).",
    )
    firmware_base_url = StringVar(
        required=False,
        description=(
            "Device-facing base URL; download_url is built as <base>/<filename>. "
            "Defaults to the FIRMWARE_BASE_URL env var, then the project default."
        ),
    )
    download_url_override = StringVar(
        required=False,
        description=(
            "Full device download URL to store verbatim (skips base + filename). "
            "Leave blank to build it from the base above."
        ),
    )
    expected_checksum = StringVar(
        required=False,
        description="Cisco-published checksum to record (and verify if requested).",
    )
    hashing_algorithm = ChoiceVar(
        choices=SoftwareImageFileHashingAlgorithmChoices.CHOICES,
        required=False,
        description="Algorithm for the checksum (required if a checksum is given).",
    )
    verify_download = BooleanVar(
        default=False,
        description=(
            "Download the full image and verify its hash. Bandwidth/time "
            "intensive; requires a checksum + algorithm."
        ),
    )
    verify_repo_tls = BooleanVar(
        default=C.REPO_VERIFY_TLS,
        description=(
            "Verify TLS when validating over HTTPS (ignored for the internal HTTP "
            "route). Off by default for a self-signed server; turn on for a "
            "CA-trusted cert."
        ),
    )
    dryrun = DryRunVar(
        description="Validate only; do not create or modify the record.",
    )

    class Meta:
        name = "Register IOS-XE Image"
        description = (
            "Validate a firmware image on the companion firmware server and record "
            "it as a core Software Image File (no upload — publish via Filebrowser "
            "first)."
        )
        has_sensitive_variables = False
        dryrun_default = True
        soft_time_limit = 5400
        time_limit = 7200
        field_order = [
            "image_file_name",
            "software_version",
            "device_types",
            "image_status",
            "default_image",
            "firmware_base_url",
            "download_url_override",
            "expected_checksum",
            "hashing_algorithm",
            "verify_download",
            "verify_repo_tls",
            "dryrun",
        ]

    def run(
        self,
        *,
        image_file_name,
        software_version,
        device_types,
        image_status,
        default_image,
        firmware_base_url,
        download_url_override,
        expected_checksum,
        hashing_algorithm,
        verify_download,
        verify_repo_tls,
        dryrun,
    ):
        file_name = (image_file_name or "").strip()
        expected_checksum = (expected_checksum or "").strip()
        if not file_name:
            raise RegisterAbort("An image file name is required.")
        if expected_checksum and not hashing_algorithm:
            raise RegisterAbort("A hashing algorithm is required when a checksum is given.")

        device_url = self._device_url(file_name, firmware_base_url, download_url_override)
        candidates = self._validation_candidates(file_name, device_url, download_url_override)
        self.logger.info("Registering '%s' (device URL: %s).", file_name, device_url)

        size, used_url = self._head_first(candidates, verify_repo_tls)
        checksum = self._maybe_verify(
            used_url, expected_checksum, hashing_algorithm, verify_download, verify_repo_tls
        )

        if dryrun:
            return (
                f"DRY-RUN ok: '{file_name}' reachable via {used_url} (size={size}). "
                f"Would store download_url={device_url} for {software_version} and "
                f"map {len(device_types or [])} device type(s)."
            )

        image = self._upsert(
            software_version=software_version,
            file_name=file_name,
            download_url=device_url,
            size=size,
            checksum=checksum,
            hashing_algorithm=hashing_algorithm,
            status=image_status,
            default_image=default_image,
            device_types=device_types,
        )
        getattr(self.logger, "success", self.logger.info)(
            f"Registered '{image.image_file_name}' for {software_version} "
            f"(download_url: {device_url}).",
            extra={"object": image},
        )
        return f"Registered '{image.image_file_name}' for {software_version}."

    # ------------------------------------------------------------------- URLs --

    @staticmethod
    def _device_url(file_name, base_override, url_override):
        """The device-facing URL stored in download_url."""
        override = (url_override or "").strip()
        if override:
            return override
        base = (base_override or "").strip()
        if not base:
            base = (os.getenv(C.FIRMWARE_BASE_URL_ENV) or "").strip() or C.FIRMWARE_BASE_URL_DEFAULT
        return f"{base.rstrip('/')}/{file_name}"

    @staticmethod
    def _validation_candidates(file_name, device_url, url_override):
        """URLs the worker tries (in order) to validate the image.

        Prefer the internal firmware-download route (always routable on the Docker
        network, plain HTTP — no self-signed-cert pain); fall back to the
        device-facing URL. If a full override URL was given, validate that only.
        """
        candidates = []
        if not (url_override or "").strip():
            internal = os.getenv(C.FIRMWARE_INTERNAL_URL_ENV)
            if internal is None:
                internal = C.FIRMWARE_INTERNAL_URL_DEFAULT
            internal = internal.strip().rstrip("/")
            if internal:
                candidates.append(f"{internal}/{file_name}")
        candidates.append(device_url)
        return candidates

    # ------------------------------------------------------------- validation --

    def _head_first(self, urls, verify_tls):
        """HEAD each URL in order; return (size_bytes_or_None, reachable_url)."""
        last_error = None
        for url in urls:
            try:
                resp = requests.head(
                    url, allow_redirects=True, timeout=C.REPO_HEAD_TIMEOUT, verify=verify_tls
                )
            except requests.RequestException as exc:
                last_error = exc
                self.logger.warning("HEAD %s failed: %s", url, exc)
                continue
            if not resp.ok:
                last_error = f"HTTP {resp.status_code}"
                self.logger.warning("HEAD %s -> HTTP %s", url, resp.status_code)
                continue
            length = resp.headers.get("Content-Length")
            size = int(length) if length and length.isdigit() else None
            if size is None:
                self.logger.warning("%s reachable but reported no Content-Length.", url)
            self.logger.info("Validated image at %s (size=%s).", url, size)
            return size, url
        raise RegisterAbort(
            f"Image not reachable at any of: {', '.join(urls)} (last error: {last_error})."
        )

    def _maybe_verify(self, url, expected, algo, verify_download, verify_tls):
        """Optionally download + hash the image; return the checksum to store."""
        if not verify_download:
            return expected or ""
        if not algo:
            raise RegisterAbort("Verify download requested but no hashing algorithm given.")
        if algo not in C.HASHLIB_SUPPORTED:
            self.logger.warning(
                "Algorithm '%s' cannot be computed in-job; recording the provided "
                "checksum without verification.", algo
            )
            return expected or ""

        self.logger.info("Downloading from %s to verify %s (this can take a while)...", url, algo)
        digest = hashlib.new(algo)
        try:
            with requests.get(
                url, stream=True, timeout=C.REPO_DOWNLOAD_TIMEOUT, verify=verify_tls
            ) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_content(chunk_size=C.REPO_CHUNK_SIZE):
                    digest.update(chunk)
        except requests.RequestException as exc:
            raise RegisterAbort(f"Failed downloading image for verification: {exc}") from exc

        computed = digest.hexdigest()
        if expected and computed.lower() != expected.strip().lower():
            raise RegisterAbort(
                f"Checksum mismatch: computed {computed}, expected {expected}. The "
                "image on the firmware server is corrupt or the wrong file."
            )
        self.logger.info("Verified %s = %s.", algo, computed)
        return computed

    # ----------------------------------------------------------------- write --

    def _upsert(
        self,
        *,
        software_version,
        file_name,
        download_url,
        size,
        checksum,
        hashing_algorithm,
        status,
        default_image,
        device_types,
    ):
        with transaction.atomic():
            image, _created = SoftwareImageFile.objects.get_or_create(
                software_version=software_version,
                image_file_name=file_name,
                defaults={"status": status, "download_url": download_url},
            )
            # Clear any existing default for this version BEFORE saving the new
            # one as default (core allows only one default image per version).
            if default_image:
                others = SoftwareImageFile.objects.filter(
                    software_version=software_version, default_image=True
                ).exclude(pk=image.pk)
                for other in others:
                    other.default_image = False
                    other.validated_save()

            image.download_url = download_url
            image.status = status
            if size is not None:
                image.image_file_size = size
            if checksum:
                image.image_file_checksum = checksum
            if hashing_algorithm:
                image.hashing_algorithm = hashing_algorithm
            image.default_image = default_image
            image.validated_save()

            if device_types:
                image.device_types.add(*device_types)

        return image
