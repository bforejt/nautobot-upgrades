"""Register a Cisco IOS-XE image into Nautobot from the firmware server.

Companion to the upgrade job. The actual .bin files are hosted by the companion
"nautobot-composer" stack's `firmware` profile: engineers upload via a
Filebrowser UI, and a read-only nginx "firmware-download" service serves the same
files to devices. Nautobot is only the index.

Given the uploaded file name, this Job builds the DEVICE-FACING download URL from
a configurable base, validates the image is reachable (preferring the worker's
internal route to the firmware-download service), optionally downloads +
hash-verifies it, and records it as a core ``dcim.SoftwareImageFile`` mapped to
the compatible device types — creating the ``dcim.SoftwareVersion`` too if one
isn't selected. It does NOT upload the file; publish it via Filebrowser first.

NOTE: brand new, not yet validated end-to-end. Run with Dry-run first.
"""

from __future__ import annotations

import hashlib
import os

import requests
from django.contrib.contenttypes.models import ContentType
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
from nautobot.dcim.models import DeviceType, Platform, SoftwareImageFile, SoftwareVersion
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
        required=False,
        description=(
            "EITHER -> the existing Software Version this image provides (leave "
            "blank to create a new one below)."
        ),
    )
    new_version = StringVar(
        required=False,
        description=(
            "OR-> create a new Software Version with this version string (e.g. "
            "17.09.04) when none is selected above."
        ),
    )
    platform = ObjectVar(
        model=Platform,
        required=True,
        description="Platform for the Software Version (used only when creating a new one).",
    )
    version_status = ObjectVar(
        model=Status,
        required=True,
        query_params={"content_types": "dcim.softwareversion"},
        description="Status for the Software Version (used only when creating a new one).",
    )
    device_types = MultiObjectVar(
        model=DeviceType,
        required=False,
        description=(
            "Device types this image is compatible with. IMPORTANT: the upgrade job "
            "can only use an image that is mapped to the device's type, assigned "
            "directly to the device, or marked Default image — set this (or Default "
            "image below) or the image will not be resolvable."
        ),
    )
    image_status = ObjectVar(
        model=Status,
        required=True,
        query_params={"content_types": "dcim.softwareimagefile"},
        description="Status for the Software Image File record.",
    )
    default_image = BooleanVar(
        default=False,
        description=(
            "Mark as the default image for this version (unsets any other). The "
            "upgrade job falls back to the default image when no device-type "
            "mapping matches — check this if you leave Device types blank."
        ),
    )
    firmware_base_url = StringVar(
        required=False,
        description=(
            "Device-facing base URL; download_url is built as <base>/<filename>. "
            "Defaults to the FIRMWARE_BASE_URL env var on the worker; required if "
            "that is unset (unless you give a full Download URL override)."
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
        description="Validate only; do not create or modify anything.",
    )

    class Meta:
        name = "Register IOS-XE Image"
        description = (
            "Validate a firmware image on the companion firmware server and record "
            "it as a core Software Image File (creating the Software Version if "
            "needed). No upload — publish via Filebrowser first."
        )
        has_sensitive_variables = False
        dryrun_default = True
        soft_time_limit = 5400
        time_limit = 7200
        field_order = [
            "image_file_name",
            "software_version",
            "new_version",
            "platform",
            "version_status",
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

    def run(self, **kwargs):
        # A custom exception raised from a Git-repo job cannot be unpickled by
        # Celery (it surfaces as UnpickleableExceptionWrapper). Translate our
        # control-flow abort into a logged error + a built-in exception, raised
        # OUTSIDE the except block so the custom exception isn't attached as the
        # new exception's __context__ (which would re-introduce the pickle issue).
        try:
            return self._execute(**kwargs)
        except RegisterAbort as exc:
            abort_message = str(exc)
        self.logger.error(abort_message)
        raise RuntimeError(abort_message)

    def _execute(
        self,
        *,
        image_file_name,
        software_version,
        new_version,
        platform,
        version_status,
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
        new_version = (new_version or "").strip()
        if not file_name:
            raise RegisterAbort("An image file name is required.")
        if expected_checksum and not hashing_algorithm:
            raise RegisterAbort("A hashing algorithm is required when a checksum is given.")

        # Resolve the target version: an existing selection wins; otherwise we
        # create one from new_version + platform + status (validated up front so a
        # dry-run reports the same errors a real run would).
        if not software_version:
            if not new_version:
                raise RegisterAbort(
                    "Provide a New version to create (Platform and Version status "
                    "are required), or select an existing Software Version."
                )
            self._check_status(version_status, SoftwareVersion, "Software Versions")
        elif new_version:
            self.logger.warning(
                "An existing Software Version is selected; ignoring the New version "
                "field (and the Platform / Version status)."
            )
        self._check_status(image_status, SoftwareImageFile, "Software Image Files")

        device_url = self._device_url(file_name, firmware_base_url, download_url_override)
        candidates = self._validation_candidates(file_name, device_url, download_url_override)
        version_label = software_version or f"new version '{new_version}' on {platform}"
        self.logger.info(
            "Registering '%s' for %s (device URL: %s).", file_name, version_label, device_url
        )

        size, used_url = self._head_first(candidates, verify_repo_tls)
        checksum = self._maybe_verify(
            used_url, expected_checksum, hashing_algorithm, verify_download, verify_repo_tls
        )

        if dryrun:
            note = self._resolvability_note(bool(device_types), default_image)
            return (
                f"DRY-RUN ok: '{file_name}' reachable via {used_url} (size={size}). "
                f"Would store download_url={device_url} for {version_label} and map "
                f"{len(device_types or [])} device type(s).{note}"
            )

        image = self._write(
            software_version=software_version,
            new_version=new_version,
            platform=platform,
            version_status=version_status,
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
            f"Registered '{image.image_file_name}' for {image.software_version} "
            f"(download_url: {device_url}).",
            extra={"object": image},
        )
        # Evaluate on the SAVED record: a re-run may inherit mappings/default from
        # a prior registration, in which case the image is already resolvable.
        note = self._resolvability_note(
            image.device_types.exists(), image.default_image, log_object=image
        )
        return f"Registered '{image.image_file_name}' for {image.software_version}.{note}"

    def _resolvability_note(self, has_device_types, is_default, log_object=None):
        """Warn when the registered image cannot be resolved by the upgrade job.

        The upgrade job resolves an image via device-assignment, the device-type
        map, or the version's default image. With no device types and no default
        flag, the record exists but no upgrade can ever use it — flag that loudly
        in the log AND in the job result rather than letting it surface later as
        an upgrade-time abort.
        """
        if has_device_types or is_default:
            return ""
        self.logger.warning(
            "Neither Device types nor Default image is set: the UPGRADE JOB WILL "
            "NOT FIND THIS IMAGE until you map it to a device type, assign it "
            "directly to a device, or mark it as the version's default image "
            "(re-run this job with those fields set, or edit the Software Image "
            "File in Nautobot).",
            extra={"object": log_object} if log_object is not None else None,
        )
        return (
            " WARNING: not yet resolvable by the upgrade job — no device-type "
            "mapping and not the default image."
        )

    # ------------------------------------------------------------------- URLs --

    @staticmethod
    def _device_url(file_name, base_override, url_override):
        """The device-facing URL stored in download_url."""
        override = (url_override or "").strip()
        if override:
            return override
        base = (base_override or "").strip() or (os.getenv(C.FIRMWARE_BASE_URL_ENV) or "").strip()
        if not base:
            raise RegisterAbort(
                "No firmware base URL configured. Set the FIRMWARE_BASE_URL "
                "environment variable on the Nautobot worker (e.g. "
                "https://<host>:9443/images/), fill the 'Firmware base URL' field, "
                "or provide a full 'Download URL override'."
            )
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
                "checksum without verification.",
                algo,
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

    @staticmethod
    def _check_status(status, model, label):
        """Confirm a Status is associated with the given model's content type."""
        content_type = ContentType.objects.get_for_model(model)
        if not status.content_types.filter(pk=content_type.pk).exists():
            raise RegisterAbort(f"Status '{status}' is not valid for {label}.")

    def _resolve_version(self, existing, new_version, platform, version_status):
        """Return the SoftwareVersion to use, creating it if one wasn't selected.

        Uses get_or_create so two concurrent runs creating the same (version,
        platform) don't race on the unique constraint. The status content type was
        already validated up front in run().
        """
        if existing:
            return existing
        version, created = SoftwareVersion.objects.get_or_create(
            version=new_version, platform=platform, defaults={"status": version_status}
        )
        self.logger.info(
            "%s Software Version '%s'.", "Created" if created else "Reusing existing", version
        )
        return version

    def _write(
        self,
        *,
        software_version,
        new_version,
        platform,
        version_status,
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
            version = self._resolve_version(software_version, new_version, platform, version_status)
            image, _created = SoftwareImageFile.objects.get_or_create(
                software_version=version,
                image_file_name=file_name,
                defaults={"status": status, "download_url": download_url},
            )

            # Clear any existing default for this version BEFORE saving the new one
            # as default (core allows only one default image per version).
            if default_image:
                others = SoftwareImageFile.objects.filter(
                    software_version=version, default_image=True
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
