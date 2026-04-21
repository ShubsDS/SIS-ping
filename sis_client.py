"""UVA SIS class-availability client.

The public SIS IScript endpoints require a PeopleSoft session cookie.
We establish one by hitting the login landing page, then call
IScript_ClassDetails with those cookies attached. If the session
expires we re-establish it transparently.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

SIS_LOGIN_URL = "https://sisuva.admin.virginia.edu/psp/ihprd/?cmd=login"
SIS_DETAILS_URL = (
    "https://sisuva.admin.virginia.edu/psc/ihprd/UVSS/SA/s/"
    "WEBLIB_HCX_CM.H_CLASS_SEARCH.FieldFormula.IScript_ClassDetails"
)
SIS_SEARCH_URL_TEMPLATE = (
    "https://sisuva.admin.virginia.edu/Shibboleth.sso/Login"
    "?target=https%3A%2F%2Fsisuva.admin.virginia.edu%2Fpsc%2Fihprd%2FEMPLOYEE%2FSA%2Fc%2F"
    "SSR_STUDENT_FL.SSR_MANAGE_CLASSES_FL.GBL%3FSTRM%3D{term}%26CLASS_NBR%3D{class_nbr}"
)

# Browser-like UA helps avoid WAF 403s. Contact note lets UVA reach you if needed.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    "SIS-ping/1.0 (+personal class-availability watcher)"
)


@dataclass
class ClassAvailability:
    """Parsed subset of the IScript_ClassDetails response."""

    class_nbr: int
    term: str
    subject: str
    catalog_nbr: str
    section: str
    title: str
    status: str  # "Open" / "Closed" / "Wait List"
    enrollment_available: int
    enrollment_total: int
    class_capacity: int
    wait_list_total: int
    wait_list_capacity: int

    @property
    def is_open(self) -> bool:
        return self.enrollment_available > 0

    @property
    def label(self) -> str:
        return f"{self.subject} {self.catalog_nbr}-{self.section} ({self.class_nbr})"


class SISClient:
    """Thin async client around the SIS IScript endpoints with session management."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._session_lock = asyncio.Lock()
        self._session_initialized = False

    @classmethod
    async def create(cls) -> "SISClient":
        """Build a client with a fresh cookie jar + browser-like headers."""
        connector = aiohttp.TCPConnector(limit=4)
        session = aiohttp.ClientSession(
            connector=connector,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
            },
            cookie_jar=aiohttp.CookieJar(),
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return cls(session)

    async def close(self) -> None:
        await self._session.close()

    async def _ensure_session(self, force: bool = False) -> None:
        """Hit the SIS login landing page to pick up session cookies."""
        async with self._session_lock:
            if self._session_initialized and not force:
                return
            # First establishment is noteworthy; subsequent re-logins are routine.
            level = logging.DEBUG if self._session_initialized else logging.INFO
            log.log(level, "Establishing SIS session...")
            async with self._session.get(SIS_LOGIN_URL, allow_redirects=True) as r:
                await r.read()
                if r.status >= 400:
                    raise RuntimeError(f"SIS login landing returned HTTP {r.status}")
            log.log(
                level,
                "SIS session established (%d cookies)",
                len(self._session.cookie_jar),
            )
            self._session_initialized = True

    async def fetch_class(self, term: str, class_nbr: int) -> ClassAvailability:
        """Fetch availability for a single class. Refreshes the session on failure."""
        await self._ensure_session()

        for attempt in (1, 2):
            params = {
                "institution": "UVA01",
                "term": str(term),
                "class_nbr": str(class_nbr),
            }
            async with self._session.get(SIS_DETAILS_URL, params=params) as resp:
                body = await resp.text()
                content_type = resp.headers.get("Content-Type", "")

                # SIS "Access Interrupted" (CSA5) page or any HTML means session died.
                looks_like_interrupt = (
                    "Access to UVA SIS Interrupted" in body
                    or "CSA5" in body
                    or body.lstrip().startswith("<")
                )

                if resp.status == 200 and "json" in content_type and not looks_like_interrupt:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception as e:  # pragma: no cover
                        raise RuntimeError(f"SIS returned non-JSON body: {e}") from e
                    return _parse_class_details(data, term=str(term))

                if looks_like_interrupt and attempt == 1:
                    # Normal: UVA's WAF invalidates sessions fairly often.
                    # We quietly re-login and retry once.
                    log.debug("SIS session expired; re-establishing and retrying")
                    await self._ensure_session(force=True)
                    continue

                raise RuntimeError(
                    f"SIS returned HTTP {resp.status} (content-type={content_type!r}): "
                    f"{body[:200]!r}"
                )

        # Unreachable, but appeases type-checkers.
        raise RuntimeError("SIS fetch failed after retry")


def _parse_class_details(data: dict, term: str) -> ClassAvailability:
    """Extract the fields we care about. Tolerant of string/int mixes."""
    section_info = data.get("section_info") or {}
    details = section_info.get("class_details") or {}
    avail = section_info.get("class_availability") or {}

    def _as_int(value, default: int = 0) -> int:
        if value in (None, ""):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return ClassAvailability(
        class_nbr=_as_int(details.get("class_number")),
        term=term,
        subject=str(details.get("subject", "")),
        catalog_nbr=str(details.get("catalog_nbr", "")),
        section=str(details.get("class_section", "")),
        title=str(details.get("course_title", "")),
        status=str(details.get("status", "")),
        enrollment_available=_as_int(avail.get("enrollment_available")),
        enrollment_total=_as_int(avail.get("enrollment_total")),
        class_capacity=_as_int(avail.get("class_capacity")),
        wait_list_total=_as_int(avail.get("wait_list_total")),
        wait_list_capacity=_as_int(avail.get("wait_list_capacity")),
    )


def sis_class_url(term: str, class_nbr: int) -> str:
    """Direct link that an authenticated user can follow to enroll."""
    return SIS_SEARCH_URL_TEMPLATE.format(term=term, class_nbr=class_nbr)
