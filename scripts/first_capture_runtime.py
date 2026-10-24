"""Shared TX2 IO construction for one finite first-capture batch."""
from capture_review_views import check_other_services
from capture_ring_views import check_additional_services
from first_capture_io import FirstCaptureIO


def interlocks():
    check_other_services()
    check_additional_services()


def io_factory(camera, project_root, console_root, references):
    return lambda unused: FirstCaptureIO(camera, project_root, console_root, references, interlocks)
