from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class PollAPIError(Exception):
    """Custom exception for PollsAPI-related failures."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body

    def __str__(self) -> str:
        parts = [self.message]
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.response_body not in (None, ""):
            parts.append(f"body={self.response_body}")
        return " | ".join(parts)


class PollHandler:
    BASE_URL = "https://api.pollsapi.com/v1"
    VALID_OPTIONS = {"yes", "maybe", "no"}

    def __init__(self, api_key: str, timeout: float = 20.0) -> None:
        if not api_key:
            raise ValueError("api_key is required")

        self.api_key = api_key
        self.timeout = timeout

        # Poll state
        self.poll_id: str | None = None
        self.poll_identifier: str | None = None
        self.poll_question: str | None = None
        self.option_ids: dict[str, str] = {}
        self.poll_metadata: dict[str, Any] = {}

        # Vote state
        self.votes_by_identifier: dict[str, dict[str, Any]] = {}
        self.identifier_by_vote_id: dict[str, str] = {}

    def create_poll(self, question: str, poll_identifier: str | None = None) -> dict:
        payload: dict[str, Any] = {
            "question": question,
            "options": [
                {"text": "yes", "data": {}},
                {"text": "maybe", "data": {}},
                {"text": "no", "data": {}},
            ],
        }
        if poll_identifier:
            payload["identifier"] = poll_identifier

        response = self._request("POST", "/create/poll", payload=payload)

        poll_doc = self._extract_poll_doc(response)
        if not poll_doc:
            raise PollAPIError(
                "Could not find poll metadata in create_poll response",
                response_body=response,
            )

        poll_id = self._extract_id(poll_doc, ("poll_id", "id", "_id"))
        if not poll_id:
            raise PollAPIError(
                "Could not determine poll_id from create_poll response",
                response_body=response,
            )

        option_ids = self._extract_option_ids(poll_doc)
        missing = self.VALID_OPTIONS - set(option_ids.keys())
        if missing:
            raise PollAPIError(
                f"Missing option IDs in create_poll response for: {', '.join(sorted(missing))}",
                response_body=response,
            )

        self.poll_id = poll_id
        self.poll_identifier = (
            poll_identifier or str(poll_doc.get("identifier") or "") or None
        )
        self.poll_question = question
        self.option_ids = option_ids
        self.poll_metadata = poll_doc

        # Reset cached votes when a new poll is created
        self.votes_by_identifier.clear()
        self.identifier_by_vote_id.clear()

        return {
            "poll_id": self.poll_id,
            "poll_identifier": self.poll_identifier,
            "question": self.poll_question,
            "option_ids": self.option_ids.copy(),
            "poll": self.poll_metadata,
        }

    def cast_vote(self, identifier: str, option: str) -> dict:
        if not self.poll_id:
            raise PollAPIError("No active poll. Call create_poll first.")

        option_key = option.strip().lower()
        if option_key not in self.VALID_OPTIONS:
            raise ValueError("option must be one of: yes, maybe, no")

        option_id = self.option_ids.get(option_key)
        if not option_id:
            raise PollAPIError(f"No stored option ID for '{option_key}'.")

        payload = {
            "poll_id": self.poll_id,
            "option_id": option_id,
            "identifier": identifier,
        }
        response = self._request("POST", "/create/vote", payload=payload)

        vote_id = self._extract_vote_id(response)
        if not vote_id:
            # Fallback: fetch identifier votes and match by poll/option
            votes = self._get_votes_with_identifier(identifier)
            for vote in votes:
                if self._vote_matches_current_poll(vote) and self._vote_option_id(vote) == option_id:
                    vote_id = self._extract_id(vote, ("vote_id", "id", "_id"))
                    if vote_id:
                        break

        if not vote_id:
            raise PollAPIError(
                "Vote appears created but vote ID could not be determined.",
                response_body=response,
            )

        vote_record = {
            "id": vote_id,
            "identifier": identifier,
            "option": option_key,
            "option_id": option_id,
        }
        self.votes_by_identifier[identifier] = vote_record
        self.identifier_by_vote_id[vote_id] = identifier
        return vote_record

    def get_all_votes(self) -> list[dict]:
        if not self.poll_id:
            raise PollAPIError("No active poll. Call create_poll first.")

        poll_id = quote(self.poll_id, safe="")
        return self._paginate_votes(f"/get/votes/{poll_id}")

    def remove_vote(self, vote_id: str) -> bool:
        response = self._request("POST", "/remove/vote", payload={"vote_id": vote_id})
        ok = self._response_successful(response)

        if ok:
            identifier = self.identifier_by_vote_id.pop(vote_id, None)
            if identifier and self.votes_by_identifier.get(identifier, {}).get("id") == vote_id:
                self.votes_by_identifier.pop(identifier, None)

        return ok

    def remove_poll(self) -> bool:
        if not self.poll_id:
            return False

        response = self._request("POST", "/remove/poll", payload={"poll_id": self.poll_id})
        ok = self._response_successful(response)

        if ok:
            self.poll_id = None
            self.poll_identifier = None
            self.poll_question = None
            self.option_ids.clear()
            self.poll_metadata = {}
            self.votes_by_identifier.clear()
            self.identifier_by_vote_id.clear()

        return ok

    def has_voted(self, identifier: str) -> bool:
        if not self.poll_id:
            raise PollAPIError("No active poll. Call create_poll first.")

        votes = self._get_votes_with_identifier(identifier)
        return any(self._vote_matches_current_poll(vote) for vote in votes)

    # ---------- Internal helpers ----------

    def _request(
        self,
        method: str,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.BASE_URL}{endpoint}"
        if params:
            url = f"{url}?{urlencode(params)}"

        headers = {
            "Accept": "application/json",
            "api-key": self.api_key,
        }

        data: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")

        request = Request(url=url, data=data, headers=headers, method=method.upper())

        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.getcode()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise PollAPIError(
                f"HTTP error for {method.upper()} {endpoint}",
                status_code=exc.code,
                response_body=body,
            ) from exc
        except URLError as exc:
            raise PollAPIError(
                f"Network error for {method.upper()} {endpoint}: {exc.reason}"
            ) from exc
        except socket.timeout as exc:
            raise PollAPIError(f"Timeout for {method.upper()} {endpoint}") from exc

        if not raw.strip():
            return {}

        try:
            response_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PollAPIError(
                f"Invalid JSON for {method.upper()} {endpoint}",
                status_code=status,
                response_body=raw,
            ) from exc

        if isinstance(response_data, dict):
            if response_data.get("success") is False:
                raise PollAPIError(
                    f"API returned success=false for {method.upper()} {endpoint}",
                    status_code=status,
                    response_body=response_data,
                )
            if str(response_data.get("status", "")).lower() in {"error", "failed"}:
                raise PollAPIError(
                    f"API returned error status for {method.upper()} {endpoint}",
                    status_code=status,
                    response_body=response_data,
                )

        return response_data

    def _paginate_votes(self, endpoint: str, limit: int = 25) -> list[dict[str, Any]]:
        offset = 0
        all_votes: list[dict[str, Any]] = []
        seen_offsets: set[int] = set()

        while True:
            if offset in seen_offsets:
                break
            seen_offsets.add(offset)

            response = self._request("GET", endpoint, params={"offset": offset, "limit": limit})
            batch = self._extract_votes(response)
            if not batch:
                break

            all_votes.extend(batch)
            if len(batch) < limit:
                break

            offset += limit

        return all_votes

    def _get_votes_with_identifier(self, identifier: str) -> list[dict[str, Any]]:
        encoded_identifier = quote(identifier, safe="")
        return self._paginate_votes(f"/get/votes-with-identifier/{encoded_identifier}")

    @staticmethod
    def _walk_dicts(value: Any) -> Iterator[dict[str, Any]]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from PollHandler._walk_dicts(child)
        elif isinstance(value, list):
            for item in value:
                yield from PollHandler._walk_dicts(item)

    @staticmethod
    def _extract_id(source: dict[str, Any], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    def _extract_poll_doc(self, response: Any) -> dict[str, Any] | None:
        if isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, dict):
                docs = data.get("docs")
                if isinstance(docs, list):
                    for item in docs:
                        if isinstance(item, dict) and isinstance(item.get("options"), list):
                            return item

                for key in ("poll", "doc"):
                    candidate = data.get(key)
                    if isinstance(candidate, dict) and isinstance(candidate.get("options"), list):
                        return candidate

                if isinstance(data.get("options"), list):
                    return data

            if isinstance(response.get("poll"), dict) and isinstance(response["poll"].get("options"), list):
                return response["poll"]

            if isinstance(response.get("options"), list):
                return response

        for obj in self._walk_dicts(response):
            if isinstance(obj.get("options"), list):
                return obj

        return None

    def _extract_option_ids(self, poll_doc: dict[str, Any]) -> dict[str, str]:
        option_ids: dict[str, str] = {}
        options = poll_doc.get("options")
        if not isinstance(options, list):
            return option_ids

        for option in options:
            if not isinstance(option, dict):
                continue

            text = str(option.get("text") or option.get("label") or option.get("name") or "").strip().lower()
            option_id = self._extract_id(option, ("option_id", "id", "_id"))
            if text in self.VALID_OPTIONS and option_id:
                option_ids[text] = option_id

        return option_ids

    def _extract_votes(self, response: Any) -> list[dict[str, Any]]:
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]

        if not isinstance(response, dict):
            return []

        data = response.get("data")
        if isinstance(data, dict):
            docs = data.get("docs")
            if isinstance(docs, list):
                return [item for item in docs if isinstance(item, dict)]

            for key in ("votes", "items", "results"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    return [item for item in candidate if isinstance(item, dict)]

        for key in ("votes", "docs", "items", "results"):
            candidate = response.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]

        return []

    def _extract_vote_id(self, response: Any) -> str | None:
        if isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, dict):
                for key in ("vote", "doc"):
                    candidate = data.get(key)
                    if isinstance(candidate, dict):
                        vote_id = self._extract_id(candidate, ("vote_id", "id", "_id"))
                        if vote_id:
                            return vote_id

                docs = data.get("docs")
                if isinstance(docs, list):
                    for item in docs:
                        if isinstance(item, dict):
                            vote_id = self._extract_id(item, ("vote_id", "id", "_id"))
                            if vote_id:
                                return vote_id

        for obj in self._walk_dicts(response):
            if any(k in obj for k in ("poll_id", "option_id", "identifier", "option")):
                vote_id = self._extract_id(obj, ("vote_id", "id", "_id"))
                if vote_id:
                    return vote_id

        return None

    @staticmethod
    def _vote_poll_id(vote: dict[str, Any]) -> str | None:
        poll_id = vote.get("poll_id")
        if poll_id:
            return str(poll_id)

        poll_obj = vote.get("poll")
        if isinstance(poll_obj, dict):
            for key in ("poll_id", "id", "_id"):
                value = poll_obj.get(key)
                if value:
                    return str(value)

        return None

    @staticmethod
    def _vote_option_id(vote: dict[str, Any]) -> str | None:
        option_id = vote.get("option_id")
        if option_id:
            return str(option_id)

        option_obj = vote.get("option")
        if isinstance(option_obj, dict):
            for key in ("option_id", "id", "_id"):
                value = option_obj.get(key)
                if value:
                    return str(value)

        return None

    def _vote_matches_current_poll(self, vote: dict[str, Any]) -> bool:
        if not self.poll_id:
            return False

        vote_poll_id = self._vote_poll_id(vote)
        if vote_poll_id and vote_poll_id == self.poll_id:
            return True

        vote_option_id = self._vote_option_id(vote)
        return bool(vote_option_id and vote_option_id in set(self.option_ids.values()))

    @staticmethod
    def _response_successful(response: Any) -> bool:
        if isinstance(response, dict):
            if isinstance(response.get("success"), bool):
                return response["success"]

            data = response.get("data")
            if isinstance(data, dict) and "deletedCount" in data:
                try:
                    return int(data["deletedCount"]) > 0
                except (TypeError, ValueError):
                    return bool(data["deletedCount"])

            status = str(response.get("status", "")).lower()
            if status in {"ok", "success"}:
                return True

        # If API doesn't provide an explicit success flag but call didn't error, treat as success.
        return True


def load_api_key() -> str | None:
    env_key = os.getenv("API_KEY")
    if env_key:
        return env_key.strip()

    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(env_path):
        return None

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                if line.startswith("export "):
                    line = line[len("export ") :].strip()

                if not line.startswith("API_KEY"):
                    continue

                if "=" not in line:
                    continue

                _, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                if value:
                    return value
    except OSError:
        return None

    return None


def main() -> int:
    api_key = load_api_key()
    if not api_key:
        print(
            "Missing API_KEY. Set env var API_KEY or add API_KEY=... to .env in current directory.",
            file=sys.stderr,
        )
        return 1

    handler = PollHandler(api_key=api_key)

    poll_identifier = f"pollhandler-demo-{uuid.uuid4().hex[:8]}"
    voters = {
        "yes": f"voter-yes-{uuid.uuid4().hex[:6]}",
        "maybe": f"voter-maybe-{uuid.uuid4().hex[:6]}",
        "no": f"voter-no-{uuid.uuid4().hex[:6]}",
    }

    try:
        print("1) Creating poll...")
        poll = handler.create_poll(
            question="Do you approve this PollHandler demo?",
            poll_identifier=poll_identifier,
        )
        print(f"   Poll created: {poll['poll_id']}")

        print("2) Casting one vote per option (yes/maybe/no)...")
        vote_records: dict[str, dict[str, Any]] = {}
        for option, identifier in voters.items():
            vote = handler.cast_vote(identifier, option)
            vote_records[option] = vote
            print(f"   {option}: vote_id={vote['id']} identifier={identifier}")

        print("3) Removing the maybe vote...")
        maybe_vote_id = vote_records["maybe"]["id"]
        maybe_removed = handler.remove_vote(maybe_vote_id)
        print(f"   Maybe vote removed: {maybe_removed}")

        print("4) Checking if first voter has already voted...")
        first_voter = voters["yes"]
        already_voted = handler.has_voted(first_voter)
        print(f"   {first_voter} has voted? {'yes' if already_voted else 'no'} ({already_voted})")

        print("5) Removing the poll...")
        poll_removed = handler.remove_poll()
        print(f"   Poll removed: {poll_removed}")

        return 0

    except PollAPIError as exc:
        print(f"Poll API error: {exc}", file=sys.stderr)

        # Best-effort cleanup
        if handler.poll_id:
            try:
                handler.remove_poll()
            except PollAPIError:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
