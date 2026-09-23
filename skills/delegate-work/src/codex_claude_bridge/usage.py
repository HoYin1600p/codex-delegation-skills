from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable


USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_DELAY_SECONDS = 15.0


class UsageError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.status_code = status_code


def _retry_delay(error: urllib.error.HTTPError, fallback_seconds: float) -> float:
    retry_after = error.headers.get("Retry-After") if error.headers is not None else None
    if isinstance(retry_after, str):
        try:
            return min(MAX_RETRY_DELAY_SECONDS, max(0.0, float(retry_after)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
                return min(MAX_RETRY_DELAY_SECONDS, max(0.0, seconds))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(MAX_RETRY_DELAY_SECONDS, max(0.0, fallback_seconds))


def _window(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsageError(f"Claude usage response omitted the {label} window")
    utilization = value.get("utilization")
    if not isinstance(utilization, (int, float)) or isinstance(utilization, bool):
        raise UsageError(f"Claude usage response has an invalid {label} utilization")
    used = max(0.0, min(100.0, float(utilization)))
    reset = value.get("resets_at")
    if reset is not None and not isinstance(reset, str):
        raise UsageError(f"Claude usage response has an invalid {label} reset time")
    return {
        "used_percent": used,
        "remaining_percent": round(100.0 - used, 2),
        "resets_at": reset,
    }


def parse_usage_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise UsageError("Claude usage response must be a JSON object")
    scoped: list[dict[str, Any]] = []
    limits = payload.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
                continue
            percent = item.get("percent")
            scope = item.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            display_name = model.get("display_name") if isinstance(model, dict) else None
            model_id = None
            if isinstance(model, dict):
                for key in ("id", "model_id", "name"):
                    candidate = model.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        model_id = candidate
                        break
            if not isinstance(percent, (int, float)) or isinstance(percent, bool) or not isinstance(display_name, str):
                continue
            used = max(0.0, min(100.0, float(percent)))
            scoped.append(
                {
                    "model": display_name,
                    "model_id": model_id,
                    "used_percent": used,
                    "remaining_percent": round(100.0 - used, 2),
                    "resets_at": item.get("resets_at") if isinstance(item.get("resets_at"), str) else None,
                }
            )
    return {
        "status": "available",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "five_hour": _window(payload.get("five_hour"), "five-hour"),
        "weekly": _window(payload.get("seven_day"), "weekly"),
        "weekly_scoped": scoped,
        "source": "claude_code_oauth_usage",
    }


def fetch_usage(
    *,
    claude_version: str,
    timeout_seconds: float = 15,
    opener: Callable[..., Any] = urllib.request.urlopen,
    max_attempts: int = 3,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    credentials_path = config_dir / ".credentials.json"
    try:
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UsageError("Claude subscription credentials were not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError("Claude subscription credentials are unreadable or invalid") from exc
    oauth = credentials.get("claudeAiOauth") if isinstance(credentials, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        raise UsageError("Claude subscription access token is unavailable")

    version_token = claude_version.split()[0] if claude_version else "external"
    request = urllib.request.Request(
        USAGE_ENDPOINT,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": f"claude-cli/{version_token}",
            "Accept": "application/json",
        },
    )
    for attempt in range(1, max_attempts + 1):
        try:
            with opener(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            result = parse_usage_payload(payload)
            result["request_attempts"] = attempt
            return result
        except urllib.error.HTTPError as exc:
            transient = exc.code in RETRYABLE_HTTP_STATUS
            if transient and attempt < max_attempts:
                sleeper(_retry_delay(exc, 2 ** (attempt - 1)))
                continue
            raise UsageError(
                f"Claude usage check failed with HTTP {exc.code} after {attempt} attempt(s)",
                transient=transient,
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            if attempt < max_attempts:
                sleeper(min(MAX_RETRY_DELAY_SECONDS, float(2 ** (attempt - 1))))
                continue
            raise UsageError(
                f"Claude usage check could not reach Anthropic after {attempt} attempt(s)",
                transient=True,
            ) from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UsageError("Claude usage check returned unreadable data") from exc
    raise AssertionError("usage retry loop exited unexpectedly")
