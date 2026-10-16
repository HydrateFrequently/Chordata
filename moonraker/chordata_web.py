# Copyright (C) 2026 Chordata contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live page and optional, advisory System One review outside Klipper's reactor."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from tornado.httpclient import AsyncHTTPClient, HTTPRequest
from ..common import RequestType, TransportType

PAGE = Path(__file__).resolve().parent.parent / "web" / "index.html"
DEFAULTS = dict(enabled=False, provider="laya", endpoint="", model="auto",
                min_confidence=0.7, api_key="")
MESSAGES = {
    "repeat": "Repeat both belts with even plucks before adjusting.",
    "inspect": "Check sensor mounting, background noise and the selected belt span.",
    "compare": "Use the measured frequencies and your printer's target to compare the belts.",
    "insufficient": "There is not enough consistent measurement evidence for a review.",
}
QUESTIONS = {"review": {
    "type": "choice",
    "instructions": "Review the quality of these belt frequency measurements. Choose a useful next check based on accepted counts, spread and rejection reasons. Frequency is not force. Never infer a safe mechanical tension or override the deterministic result.",
    "criteria": {
        "repeat": "Partial series, inconsistent frequencies or too many rejected plucks: repeat both belts.",
        "inspect": "Repeated weak tones, knocks or interference suggest checking the measurement setup.",
        "compare": "Both series have enough consistent accepted plucks to compare against the user-supplied target.",
        "insufficient": "Missing, stale or ambiguous evidence prevents a useful review.",
    },
}}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_settings(data, previous=None):
    """Validate before storing; never forward an old secret to a new destination."""
    if not isinstance(data, dict):
        raise ValueError("Settings must be an object")
    old = dict(DEFAULTS, **(previous or {}))
    new = dict(old)
    for key in DEFAULTS:
        if key in data:
            new[key] = data[key]
    if type(new["enabled"]) is not bool or new["provider"] not in ("laya", "jev"):
        raise ValueError("Choose Laya or Jev and an enabled boolean")
    endpoint = new["endpoint"]
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise ValueError("Enter the full HTTP(S) inference endpoint")
    endpoint = endpoint.strip()
    if endpoint:
        try:
            url = urlsplit(endpoint)
            _ = url.port
            valid = (url.scheme in ("http", "https") and url.hostname
                     and not url.username and not url.password and not url.query
                     and not url.fragment and not re.search(r"[\s\\]", endpoint))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use an HTTP(S) endpoint without credentials, query or fragment")
    if new["enabled"] and not endpoint:
        raise ValueError("An endpoint is required when AI review is enabled")
    new["endpoint"] = endpoint
    if (endpoint, new["provider"]) != (old["endpoint"], old["provider"]):
        new["api_key"] = data.get("api_key", "")
    if not isinstance(new["api_key"], str) or len(new["api_key"]) > 4096 or re.search(r"[\x00-\x20\x7f]", new["api_key"]):
        raise ValueError("API key must be a single token without whitespace")
    if not isinstance(new["model"], str) or not re.fullmatch(r"[A-Za-z0-9_./:-]{1,100}", new["model"]):
        raise ValueError("Enter a model name")
    if not finite(new["min_confidence"]) or not 0 <= new["min_confidence"] <= 1:
        raise ValueError("Minimum confidence must be between 0 and 1")
    return new


def measurement_summary(status):
    """Allowlist numbers and rejection labels; exclude paths, config and raw samples."""
    b = status.get("belts") or {}
    out = {"target_hz": b.get("target"), "tolerance_hz": b.get("tol"),
           "required_plucks": b.get("plucks"), "round": b.get("round"),
           "pair_complete": not bool(b.get("heard")), "belts": {}}
    for key in ("target_hz", "tolerance_hz", "required_plucks", "round"):
        if not finite(out[key]):
            out[key] = None
    reasons = {"no belt tone", "too weak", "a knock, not a belt",
               "the other belt was ringing along", "far from the others", "more than needed"}
    for belt in ("A", "B"):
        result = b.get(belt) or {}
        plucks = result.get("plucks") or []
        accepted = [p["freq"] for p in plucks if p.get("ok") and finite(p.get("freq"))]
        rejected = {}
        for p in plucks:
            if not p.get("ok"):
                why = p.get("why")
                why = why if why in reasons else "other rejection"
                rejected[why] = rejected.get(why, 0) + 1
        out["belts"][belt] = {
            "frequency_hz": result.get("freq") if finite(result.get("freq")) else None,
            "accepted": result.get("used") if finite(result.get("used")) else 0,
            "heard": result.get("heard") if finite(result.get("heard")) else 0,
            "measured_at": result.get("measured_at") if finite(result.get("measured_at")) else None,
            "spread_hz": max(accepted) - min(accepted) if accepted else None,
            "rejections": rejected,
        }
    return out


def decode_review(payload, threshold, summary):
    answer = payload.get("answers", {}).get("review", {})
    choice, confidence = answer.get("choice"), answer.get("confidence")
    probabilities = answer.get("probabilities")
    if (answer.get("type") != "choice" or choice not in MESSAGES
            or not finite(confidence) or not 0 <= confidence <= 1
            or not isinstance(probabilities, dict) or set(probabilities) != set(MESSAGES)
            or any(not finite(v) or not 0 <= v <= 1 for v in probabilities.values())
            or abs(sum(probabilities.values()) - 1) > .02):
        raise ValueError("Invalid review response")
    # A model cannot turn an incomplete series into a completed measurement.
    enough = summary["pair_complete"] and all(
        x["frequency_hz"] is not None and x["accepted"] >= (summary["required_plucks"] or 5)
        for x in summary["belts"].values())
    abstain = confidence < threshold or answer.get("low_confidence") or (choice == "compare" and not enough)
    return {"status": "abstained" if abstain else "ok", "decision": None if abstain else choice,
            "confidence": confidence, "probabilities": probabilities,
            "message": "AI review is uncertain; use the measured data or repeat the series." if abstain else MESSAGES[choice],
            "measurement": summary}


class ChordataWeb:
    def __init__(self, config):
        self.server = config.get_server()
        self.page = Path(os.path.expanduser(config.get("page", str(PAGE))))
        self.settings_path = Path(self.server.get_app_args()["data_path"]) / "chordata-ai.json"
        self.server.lookup_component("file_manager").add_reserved_path(
            "chordata_ai", self.settings_path, False)
        self.settings = dict(DEFAULTS)
        self.lock = asyncio.Lock()
        self.last_request = -float("inf")
        self.http = AsyncHTTPClient(force_instance=True, max_buffer_size=65536)
        try:
            if self.settings_path.exists():
                self.settings = validate_settings(json.loads(self.settings_path.read_text()))
        except (ValueError, OSError, TypeError):
            self.server.add_warning("Chordata AI settings could not be loaded; AI review is disabled.")
        self.server.register_endpoint("/server/chordata", RequestType.GET, self._handle_page,
                                      transports=TransportType.HTTP, wrap_result=False,
                                      content_type="text/html; charset=UTF-8", auth_required=False)
        self.server.register_endpoint("/access/chordata_ai", RequestType.POST, self._handle_ai)
        self.server.register_endpoint("/server/chordata/evaluate", RequestType.POST, self._evaluate)

    async def _handle_page(self, web_request):
        return await self.server.get_event_loop().run_in_thread(self.page.read_bytes)

    def public_settings(self):
        return dict({k: v for k, v in self.settings.items() if k != "api_key"},
                    has_api_key=bool(self.settings["api_key"]))

    async def _handle_ai(self, web_request):
        args = web_request.get_args()
        action = args.get("action", "get")
        if action == "get":
            return self.public_settings()
        if action != "configure":
            raise self.server.error("Unknown settings action", 400)
        try:
            settings = validate_settings(args, self.settings)
            # Small private atomic file outside Moonraker's downloadable config root.
            fd, name = tempfile.mkstemp(prefix=".chordata-ai-", dir=str(self.settings_path.parent))
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(settings, stream, allow_nan=False)
                os.replace(name, self.settings_path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            self.settings = settings
        except ValueError as exc:
            raise self.server.error(str(exc), 400)
        except OSError:
            raise self.server.error("Could not save private AI settings", 500)
        return self.public_settings()

    async def _snapshot(self):
        status = await self.server.lookup_component("klippy_apis").query_objects({"chordata": None})
        chordata = status.get("chordata", {})
        if chordata.get("state") != "idle":
            raise ValueError("Wait for the recording to finish")
        return measurement_summary(chordata)

    async def _evaluate(self, web_request):
        settings = dict(self.settings)
        if not settings["enabled"]:
            return {"status": "disabled", "message": "AI review is off. Measurement works without it."}
        if self.lock.locked() or time.monotonic() - self.last_request < 2:
            return {"status": "unavailable", "message": "A review is already running or was just requested."}
        async with self.lock:
            self.last_request = time.monotonic()
            try:
                summary = await self._snapshot()
                if not any(x["heard"] for x in summary["belts"].values()):
                    return {"status": "abstained", "message": "Measure a belt before requesting review."}
                body = {"state": summary, "questions": QUESTIONS}
                if settings["provider"] == "jev":
                    body["model"] = "jev-latest" if settings["model"] == "auto" else settings["model"]
                elif settings["model"] != "auto":
                    body["model"] = settings["model"]
                headers = {"Content-Type": "application/json", "Accept": "application/json"}
                if settings["api_key"]:
                    headers["Authorization"] = "Bearer " + settings["api_key"]
                response = await self.http.fetch(HTTPRequest(
                    settings["endpoint"], method="POST", headers=headers,
                    body=json.dumps(body, allow_nan=False), connect_timeout=3,
                    request_timeout=10, follow_redirects=False, validate_cert=True))
                result = decode_review(json.loads(response.body), settings["min_confidence"], summary)
                if settings != self.settings or summary != await self._snapshot():
                    return {"status": "abstained", "message": "Measurements or settings changed during review. Request a fresh review."}
                return result
            except Exception:
                # Provider exceptions can contain URLs, headers or echoed secrets.
                return {"status": "unavailable", "message": "AI review unavailable. Check the endpoint, credentials and service. Measurement is unaffected."}

    async def close(self):
        self.http.close()


def load_component(config):
    return ChordataWeb(config)
