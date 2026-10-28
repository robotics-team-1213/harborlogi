import ipaddress
import json
import math
import re
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class FleetIdentityError(ValueError):
    """The configured app identity was not authenticated; no operation was sent."""


class RosyClient:
    def __init__(self, base_url=None, token=None, *, timeout=10, transport=None):
        if transport is not None:
            self._request = transport
            return
        parsed = urlsplit(base_url or "")
        try:
            loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
                or parsed.scheme == "http" and not loopback):
            raise ValueError("Expected an HTTPS Fleet origin, or loopback HTTP")
        if not isinstance(token, str) or not token or any(ord(c) < 32 for c in token):
            raise ValueError("Expected a configured bearer credential")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Expected a positive finite HTTP timeout")
        self._origin, self._token, self._timeout = base_url.rstrip("/"), token, timeout
        self._opener = build_opener(_NoRedirect())
        self._request = self._http

    def _http(self, method, path, body):
        data = json.dumps(body, allow_nan=False).encode() if body is not None else None
        request = Request(self._origin + path, data=data, method=method,
                          headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        with self._opener.open(request, timeout=self._timeout) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Fleet response exceeds 1 MiB")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Expected a Fleet response object")
        return value

    def session(self):
        return self._request('GET', '/api/fleet/session', None)

    def _call(self, method, path, body):
        expected = getattr(self, 'expected_principal', None)
        if expected is not None:
            identity = self.session()
            role = getattr(self, 'expected_role', None)
            if (not isinstance(identity, dict) or identity.get('principal_id') != expected
                    or identity.get('role') not in ('service', 'operator')
                    or role is not None and identity.get('role') != role):
                raise FleetIdentityError('Configured Fleet application identity was not verified')
        return self._request(method, path, body)

    def compile(self, binding):
        result = self._call("POST", "/api/fleet/cell-app/compile", binding.compile_request())
        candidate, summary = result.get("candidate", {}), result.get("summary", {})
        if (candidate.get("kind") != "cell_job" or candidate.get("recipe_sha256") != binding.recipe_digest
                or candidate.get("cell_sha256") != binding.cell_digest
                or result.get("process_artifact_digest") != binding.process_artifact_digest
                or type(summary.get("transfer_count")) is not int or summary["transfer_count"] != 1
                or not re.fullmatch(r"[0-9a-f]{64}", result.get("process_artifact_digest", ""))):
            raise ValueError("Installed documents do not compile to the bound single-transfer process")
        return result

    def propose(self, binding, *, request_key):
        return self._call("POST", "/api/fleet/cell-app/proposals", binding.proposal_request(request_key))

    def job(self, mission_id):
        return self._call("GET", "/api/fleet/cell-jobs/" + quote(mission_id, safe=""), None)

    def trip(self, candidates, steps, *, request_key):
        return self._call("POST", "/api/fleet/trip-jobs", {
            "candidate_robot_ids": list(candidates), "request_key": request_key, "steps": list(steps),
        })

    def trip_job(self, mission_id):
        return self._call("GET", "/api/fleet/trip-jobs/" + quote(mission_id, safe=""), None)

    def trip_request(self, request_key):
        return self._call("GET", "/api/fleet/trip-jobs?request_key=" + quote(request_key, safe=""), None)

    def propose_arm(self, binding, *, request_key):
        return self._arm_response(self._call("POST", "/api/fleet/arm-transfers/jobs", binding.proposal_request(request_key)))

    def _arm_response(self, result):
        principal = getattr(self, 'expected_principal', None)
        if principal is not None and result.get('job', {}).get('proposal_principal_id') != principal:
            raise ValueError('Fleet proposal principal differs')
        return result

    def arm_job(self, mission_id):
        return self._arm_response(self._call("GET", "/api/fleet/arm-transfers/jobs/" + quote(mission_id, safe=""), None))

    def arm_request(self, request_key):
        return self._arm_response(self._call("GET", "/api/fleet/arm-transfers/jobs?request_key=" + quote(request_key, safe=""), None))

    def physical_receipt(self, mission_id):
        return self._call("GET", "/api/fleet/arm-transfers/jobs/" + quote(mission_id, safe="") + "/physical-receipt", None)

    def physical_evidence(self, mission_id):
        return self._call("GET", "/api/fleet/arm-transfers/jobs/" + quote(mission_id, safe="") + "/physical-evidence", None)

    def cancel_arm(self, mission_id, *, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 192 or any(ord(c) < 32 for c in reason):
            raise ValueError('Expected a short cancellation reason')
        return self._call('POST', '/api/fleet/arm-transfers/jobs/' + quote(mission_id, safe='') + '/cancel', {'reason': reason})
